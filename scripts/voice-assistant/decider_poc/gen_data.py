#!/usr/bin/env python3
"""Synthetic data for the Kivi decider PoC: homes, command utterances with gold labels, negatives.

Every item is generated, so its gold label (kind, intent, target option, value) is known. Homes are
the fake inventory of agent_eval/inventory_es.yaml ("base") plus random homes built from Spanish
room and device name pools. Hold-outs:
  * template families: each intent's families are split train / dev / test (no family is shared);
  * names: room and device names are split into train / dev / test pools, so dev and test homes
    use names never seen in training (the base home is a test home with common names).
No real names: the pools are generic Spanish words written for this PoC.

Output (JSON lines + one YAML inventory per home):
  <out>/homes/<home_id>.yaml        inventory in the shape of agent_eval/inventory_es.yaml
  <out>/items_<split>.jsonl          {id, split, home, family, kind, intent, target, value,
                                      spoken, written}
`spoken` has numbers as words (for Piper); `written` has digits (as Whisper tends to write them).

  python gen_data.py --out $POC/data --seed 7
"""
import argparse
import json
import random
import re
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
import numbers_es as ne  # noqa: E402
import schema as sc  # noqa: E402

BASE_INVENTORY = HERE.parent / "agent_eval" / "inventory_es.yaml"

# ---- name pools: (name, gender m/f, aliases). Split by hand into train / dev / test. -----------------
ROOMS = {
    "train": [("Salón", "m", ["sala"]), ("Cocina", "f", []), ("Dormitorio", "m", ["habitación"]),
              ("Baño", "m", ["aseo"]), ("Terraza", "f", []), ("Despacho", "m", ["oficina"]),
              ("Comedor", "m", []), ("Pasillo", "m", []), ("Entrada", "f", []), ("Garaje", "m", []),
              ("Jardín", "m", []), ("Ático", "m", []), ("Lavadero", "m", []), ("Estudio", "m", []),
              ("Cuarto de invitados", "m", []), ("Dormitorio principal", "m", []), ("Patio", "m", []),
              ("Balcón", "m", []), ("Sala de estar", "f", []), ("Cuarto de los niños", "m", [])],
    "dev": [("Sótano", "m", []), ("Vestidor", "m", []), ("Biblioteca", "f", []), ("Recibidor", "m", ["hall"]),
            ("Trastero", "m", []), ("Salita", "f", [])],
    "test": [("Buhardilla", "f", []), ("Gimnasio", "m", []), ("Bodega", "f", []), ("Porche", "m", []),
             ("Galería", "f", []), ("Taller", "m", []), ("Sala de juegos", "f", []),
             ("Habitación del fondo", "f", [])],
}
DEVICES = {
    "train": {
        "light": [("Luz del techo", "f", []), ("Lámpara de pie", "f", ["lámpara"]), ("Lámpara de lectura", "f", []),
                  ("Tira LED", "f", ["tira de luces"]), ("Foco", "m", []), ("Plafón", "m", []),
                  ("Luz de la mesa", "f", []), ("Lamparita de noche", "f", ["mesilla"]), ("Luz del espejo", "f", []),
                  ("Lámpara colgante", "f", []), ("Luz de la encimera", "f", []), ("Luz ambiental", "f", [])],
        "switch": [("Cafetera", "f", []), ("Calentador", "m", []), ("Estufa", "f", []), ("Deshumidificador", "m", []),
                   ("Tostadora", "f", [])],
        "fan": [("Ventilador", "m", []), ("Ventilador de techo", "m", [])],
        "media_player": [("Tele", "f", ["televisión"]), ("Altavoz", "m", []), ("Barra de sonido", "f", [])],
        "climate": [("Aire acondicionado", "m", ["aire"]), ("Calefacción", "f", [])],
        "cover": [("Persiana", "f", []), ("Toldo", "m", []), ("Cortina", "f", [])],
    },
    "dev": {
        "light": [("Bombilla", "f", []), ("Luz indirecta", "f", []), ("Lámpara de araña", "f", []),
                  ("Luz del armario", "f", [])],
        "switch": [("Termo", "m", []), ("Radiador", "m", [])],
        "fan": [("Ventilador de pie", "m", [])],
        "media_player": [("Televisor", "m", []), ("Equipo de música", "m", [])],
        "climate": [("Termostato", "m", [])],
        "cover": [("Estor", "m", []), ("Persiana grande", "f", [])],
    },
    "test": {
        "light": [("Flexo", "m", []), ("Guirnalda", "f", []), ("Aplique de pared", "m", ["aplique"]),
                  ("Lámpara de sal", "f", []), ("Luz del cabecero", "f", []), ("Farolillo", "m", [])],
        "switch": [("Hervidor", "m", []), ("Purificador", "m", [])],
        "fan": [("Extractor", "m", [])],
        "media_player": [("Proyector", "m", []), ("Radio", "f", [])],
        "climate": [("Bomba de calor", "f", ["bomba"])],
        "cover": [("Claraboya", "f", []), ("Contraventana", "f", [])],
    },
}
FEM_ART = {"f": "la", "m": "el", "fp": "las", "mp": "los"}


# ---- homes --------------------------------------------------------------------------------------

def slug(s):
    s = ne.fold(s)
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def random_home(rng, pool_split, hid):
    """3..6 areas, 4..12 controllable devices; names drawn from the pool of one split (with the
    train pool's common rooms mixed into dev/test homes only through the base home)."""
    rooms = rng.sample(ROOMS[pool_split], k=min(len(ROOMS[pool_split]), rng.randint(3, 6)))
    areas, gender = {}, {}
    for name, g, aliases in rooms:
        aid = slug(name)
        areas[aid] = {"name": name, "floor": "planta_baja", "aliases": list(aliases)}
        gender[f"area:{aid}"] = g
    ents = {}
    pools = DEVICES[pool_split]
    aids = list(areas)
    # Each area gets at least one light; covers, media, climate, fan and switches spread randomly.
    for aid in aids:
        for _ in range(rng.choice([1, 1, 2])):
            _add(rng, ents, gender, "light", pools["light"], aid)
    for d, p in (("cover", 0.6), ("media_player", 0.45), ("climate", 0.35), ("fan", 0.25), ("switch", 0.35)):
        for aid in aids:
            if rng.random() < p:
                _add(rng, ents, gender, d, pools[d], aid)
    sat = rng.choice(aids)
    home = {"now": "2026-11-05T18:42:00+01:00", "timezone": "Europe/Madrid", "satellite_area": sat,
            "floors": {"planta_baja": {"name": "Planta baja"}}, "areas": areas, "entities": ents,
            "gender": gender, "pool": pool_split, "id": hid}
    return home


def _add(rng, ents, gender, d, pool, aid):
    name, g, aliases = rng.choice(pool)
    eid = f"{d}.{aid}_{slug(name)}"
    if eid in ents:
        return
    e = {"name": name, "area": aid, "aliases": list(aliases)}
    if d == "media_player":
        e.update(state="playing", device_class="tv" if name.startswith("Tele") else "speaker",
                 attributes={"volume_level": 0.3})
    elif d == "cover":
        e.update(state="open", device_class="awning" if name == "Toldo" else "blind",
                 attributes={"current_position": 100})
    elif d == "climate":
        e.update(state="heat", attributes={"temperature": 21, "current_temperature": 20.0})
    else:
        e["state"] = "off"
    ents[eid] = e
    gender[eid] = g


BASE_GENDER = {"light.salon_techo": "f", "light.salon_lampara": "f", "light.cocina_techo": "f",
               "light.cocina_encimera": "f", "light.dormitorio_techo": "f", "light.dormitorio_noche": "f",
               "light.bano": "f", "cover.salon_persiana": "f", "cover.dormitorio_persiana": "f",
               "cover.terraza_toldo": "m", "climate.salon_aire": "m", "climate.dormitorio_aire": "m",
               "media_player.tele_salon": "f", "media_player.altavoz_cocina": "m", "switch.cafetera": "f",
               "fan.dormitorio_ventilador": "m", "area:salon": "m", "area:cocina": "f", "area:terraza": "f",
               "area:dormitorio": "m", "area:bano": "m"}


def base_home():
    inv = yaml.safe_load(open(BASE_INVENTORY, encoding="utf-8"))
    inv["gender"] = dict(BASE_GENDER)
    inv["pool"] = "base"
    inv["id"] = "base"
    return inv


# ---- referring expressions ----------------------------------------------------------------------

def lower_name(n):
    return n[0].lower() + n[1:] if n else n


def np_entity(home, eid, form):
    e = home["entities"][eid]
    g = home["gender"].get(eid, "f")
    name = e["name"] if form != "alias" else e["aliases"][0]
    if name.lower().startswith(("la ", "el ")):
        base = lower_name(name)
    else:
        base = f"{FEM_ART[g]} {lower_name(name)}"
    if form == "name+area":
        base += " " + de(np_area(home, e["area"]))
    return base, g


def np_area(home, aid):
    g = home["gender"].get(f"area:{aid}", "m")
    return f"{FEM_ART[g]} {lower_name(home['areas'][aid]['name'])}"


def de(np):
    return "del " + np[3:] if np.startswith("el ") else "de " + np


def a_(np):
    return "al " + np[3:] if np.startswith("el ") else "a " + np


def en(np):
    return "en " + np


def unique_name(home, eid, alias=False):
    """Can the name (or first alias) alone resolve to this entity? Mirrors FakeHome.resolve:
    unique among entities, or the only one with that name in the satellite area."""
    e = home["entities"][eid]
    n = (e["aliases"][0] if alias else e["name"]).casefold()
    same = [i for i, x in home["entities"].items()
            if n in [x["name"].casefold()] + [a.casefold() for a in x.get("aliases") or []]]
    if len(same) == 1:
        return True
    sat = [i for i in same if home["entities"][i].get("area") == home["satellite_area"]]
    return sat == [eid]


def expressions(home, intent, opt, rng):
    """(np, gender/number, implicit) choices that refer to option `opt` in this home."""
    k = opt.key
    out = []
    if k.startswith("e:"):
        eid = k[2:]
        e = home["entities"][eid]
        if unique_name(home, eid):
            out.append(np_entity(home, eid, "name"))
        out.append(np_entity(home, eid, "name+area"))
        if e.get("aliases") and unique_name(home, eid, alias=True):
            out.append(np_entity(home, eid, "alias"))
        # implicit: the only compatible device of its domain in the satellite area (media, climate, fan)
        d = sc.domain(eid)
        if d in ("media_player", "climate", "fan") and e.get("area") == home["satellite_area"]:
            same = [i for i, x in home["entities"].items() if sc.domain(i) == d and x.get("area") == e["area"]]
            if same == [eid]:
                out.append(("", home["gender"].get(eid, "f"), True))
        return [(np, g, False) if len(t) == 2 else t for t in out for np, g in [t[:2]]]
    if k.startswith("h:"):
        return [("todas las luces de la casa", "fp", False), ("todas las luces", "fp", False),
                ("las luces de toda la casa", "fp", False)]
    _, aid, g = k.split(":")
    area = np_area(home, aid)
    if g == "light":
        out = [(f"las luces {de(area)}", "fp", False), (f"la luz {de(area)}", "f", False),
               (f"todas las luces {de(area)}", "fp", False)]
        if aid == home["satellite_area"]:
            out += [("la luz", "f", False), ("las luces", "fp", False)]
        return out
    if g == "cover":
        n = sum(1 for i in opt.entities)
        words = {"Toldo": ("el toldo", "m")}
        if n == 1:
            return []  # same set as the entity option; generated through the entity
        out = [(f"las persianas {de(area)}", "fp", False), (f"todas las persianas {de(area)}", "fp", False)]
        if aid == home["satellite_area"]:
            out.append(("las persianas", "fp", False))
        del words
        return out
    return []  # "all" groups are only used by templates that say "todo"


def agree(stem, g):
    return stem + {"f": "a", "m": "o", "fp": "as", "mp": "os"}[g]


def pron(g):
    return {"f": "la", "m": "lo", "fp": "las", "mp": "los"}[g]


# ---- templates ----------------------------------------------------------------------------------
# Each family: (family id, [template strings], flags). Placeholders: {t} target NP, {de_t}, {a_t},
# {en_t}; {adj:stem} agreement with the target; {lo} pronoun; {area} / {de_area} / {en_area} the
# target's area (only for group "all" templates); {pct} {temp} {dur} values.
# flags: "imp" = the target may be implicit (empty), "all" = targets a whole area ("todo").

T = {
    "turn_on": [
        ("on_enciende", ["Enciende {t}", "Enciende {t}, por favor", "Kivi, enciende {t}"], "imp_light"),
        ("on_prende", ["Prende {t}", "Préndeme {t}"], "imp_light"),
        ("on_puedes", ["¿Puedes encender {t}?", "¿Me puedes encender {t}?", "¿Podrías encender {t}?"], ""),
        ("on_me", ["¿Me enciendes {t}?", "Enciéndeme {t}", "Enciéndeme {t}, porfa"], "imp_light"),
        ("on_quiero", ["Quiero {t} {adj:encendid}", "Necesito {t} {adj:encendid}", "Deja {t} {adj:encendid}"], ""),
        ("on_activa", ["Activa {t}", "Conecta {t}", "Pon en marcha {t}"], ""),
        ("on_da", ["Da {t}", "Dale a {t}", "Dame {t}"], "imp_light"),
        ("on_favor", ["Haz el favor de encender {t}", "A ver, enciende {t}", "Oye, enciende {t} ya"], ""),
    ],
    "turn_off": [
        ("off_apaga", ["Apaga {t}", "Apaga {t}, por favor", "Kivi, apaga {t}"], "imp_light"),
        ("off_puedes", ["¿Puedes apagar {t}?", "¿Me puedes apagar {t}?", "¿Podrías apagar {t}?"], ""),
        ("off_me", ["¿Me apagas {t}?", "Apágame {t}", "Apágame {t}, porfa"], "imp_light"),
        ("off_quita", ["Quita {t}", "Desconecta {t}", "Corta {t}"], ""),
        ("off_quiero", ["Quiero {t} {adj:apagad}", "Deja {t} {adj:apagad}", "No quiero {t} {adj:encendid}"], ""),
        ("off_todo", ["Apágalo todo {en_area}", "Apaga todo {en_area}", "Apaga todo lo {de_area}"], "all"),
        ("off_favor", ["Haz el favor de apagar {t}", "A ver, apaga {t}", "Oye, apaga {t} ya"], ""),
        ("off_fuera", ["Fuera {t}", "Ya no necesito {t}, apága{lo}", "Apaga {t} que ya me voy"], ""),
    ],
    "open_cover": [
        ("oc_abre", ["Abre {t}", "Abre {t}, por favor", "Kivi, abre {t}"], "imp"),
        ("oc_sube", ["Sube {t}", "Súbeme {t}", "Sube {t} del todo"], "imp"),
        ("oc_puedes", ["¿Puedes abrir {t}?", "¿Me subes {t}?", "¿Podrías subir {t}?"], ""),
        ("oc_levanta", ["Levanta {t}", "Recoge {t}", "Quiero {t} {adj:abiert}"], ""),
        ("oc_luz", ["Deja entrar la luz, abre {t}", "Que entre luz, sube {t}", "Abre {t} que no se ve nada"], ""),
    ],
    "close_cover": [
        ("cc_cierra", ["Cierra {t}", "Cierra {t}, por favor", "Kivi, cierra {t}"], "imp"),
        ("cc_baja", ["Baja {t}", "Bájame {t}", "Baja {t} del todo"], "imp"),
        ("cc_puedes", ["¿Puedes cerrar {t}?", "¿Me bajas {t}?", "¿Podrías bajar {t}?"], ""),
        ("cc_echa", ["Echa {t}", "Quiero {t} {adj:cerrad}", "Deja {t} {adj:bajad}"], ""),
        ("cc_sol", ["Entra mucho sol, baja {t}", "Que no entre luz, cierra {t}", "Cierra {t} que hace frío"], ""),
    ],
    "cover_position": [
        ("cp_pon", ["Pon {t} al {pct}", "Pon {t} al {pct} por ciento", "Deja {t} al {pct} por ciento"], ""),
        ("cp_sube", ["Sube {t} hasta el {pct}", "Baja {t} hasta el {pct} por ciento", "Abre {t} al {pct}"], ""),
        ("cp_mitad", ["Pon {t} a la mitad", "Deja {t} a la mitad", "Baja {t} hasta la mitad"], ""),
        ("cp_posicion", ["Posición {de_t} al {pct}", "Ajusta {t} al {pct} por ciento",
                         "¿Puedes poner {t} al {pct}?"], ""),
    ],
    "stop_cover": [
        ("sc_para", ["Para {t}", "Para {t}, por favor", "Kivi, para {t}"], "imp"),
        ("sc_deten", ["Detén {t}", "Detén {t} ya", "Que se pare {t}"], ""),
        ("sc_stop", ["Stop {t}", "Ya, para {t}", "Vale, para {t} ahí"], ""),
    ],
    "light_set": [
        ("ls_pon", ["Pon {t} al {pct}", "Pon {t} al {pct} por ciento", "Deja {t} al {pct} por ciento"], "imp_light"),
        ("ls_brillo", ["Sube el brillo {de_t} al {pct}", "Baja el brillo {de_t} al {pct}",
                       "Brillo {de_t} al {pct} por ciento"], "imp_light"),
        ("ls_max", ["Pon {t} al máximo", "Pon {t} a tope", "Sube {t} al máximo"], ""),
        ("ls_ajusta", ["Ajusta {t} al {pct} por ciento", "Atenúa {t} al {pct}", "Regula {t} al {pct}"], ""),
        ("ls_intensidad", ["Pon la intensidad {de_t} en {pct}", "Intensidad {de_t} al {pct}",
                           "¿Puedes poner {t} al {pct}?"], ""),
    ],
    "set_volume": [
        ("sv_pon", ["Pon el volumen {de_t} al {pct}", "Pon el volumen {de_t} a {pct}", "Volumen {de_t} al {pct}"],
         "imp"),
        ("sv_sube", ["Sube el volumen {de_t} a {pct}", "Baja el volumen {de_t} a {pct}",
                     "Ajusta el volumen {de_t} a {pct}"], "imp"),
        ("sv_deja", ["Deja {t} a volumen {pct}", "Pon {t} a {pct} de volumen", "¿Puedes poner {t} al {pct}?"], ""),
    ],
    "volume_up": [
        ("vu_sube", ["Sube el volumen {de_t}", "Sube el volumen {de_t}, por favor", "Kivi, sube el volumen {de_t}"],
         "imp"),
        ("vu_alto", ["Ponme {t} más {adj:alt}", "Pon {t} más {adj:alt}", "Sube {t}"], ""),
        ("vu_oye", ["No se oye {t}", "Más volumen {en_t}", "Sube un poco {t}"], "imp"),
    ],
    "volume_down": [
        ("vd_baja", ["Baja el volumen {de_t}", "Baja el volumen {de_t}, por favor", "Kivi, baja el volumen {de_t}"],
         "imp"),
        ("vd_bajito", ["Ponme {t} más {adj:bajit}", "Pon {t} más {adj:baj}", "Baja {t}"], ""),
        ("vd_alto", ["Está muy {adj:alt} {t}", "Menos volumen {en_t}", "Baja un poco {t}"], ""),
    ],
    "mute": [
        ("mu_silencia", ["Silencia {t}", "Silencia {t}, por favor", "Kivi, silencia {t}"], "imp"),
        ("mu_quita", ["Quita el sonido {de_t}", "Pon {t} en silencio", "Mutea {t}"], ""),
        ("mu_calla", ["Calla {t}", "Que se calle {t}", "Sin sonido {en_t}"], ""),
    ],
    "unmute": [
        ("um_quita", ["Quita el silencio {de_t}", "Quita el silencio {de_t}, por favor", "Kivi, quita el silencio"],
         "imp"),
        ("um_activa", ["Activa el sonido {de_t}", "Vuelve a poner el sonido {de_t}", "Desmutea {t}"], ""),
        ("um_suene", ["Que vuelva a sonar {t}", "Devuélve{lo} el sonido a {t}", "Pon sonido {en_t}"], ""),
    ],
    "pause": [
        ("pa_pausa", ["Pausa {t}", "Pausa {t}, por favor", "Kivi, pausa {t}"], "imp"),
        ("pa_pon", ["Pon {t} en pausa", "Pon en pausa {t}", "Deja {t} en pausa"], ""),
        ("pa_para", ["Para {t} un momento", "Detén {t} un momento", "Espera, pausa {t}"], ""),
    ],
    "resume": [
        ("re_reanuda", ["Reanuda {t}", "Reanuda {t}, por favor", "Kivi, reanuda {t}"], "imp"),
        ("re_sigue", ["Sigue {t}", "Continúa con {t}", "Que siga {t}"], ""),
        ("re_play", ["Dale al play {en_t}", "Quita la pausa {de_t}", "Vuelve a poner {t}"], ""),
    ],
    "next": [
        ("ne_siguiente", ["Siguiente canción {en_t}", "Pon la siguiente canción {en_t}", "Siguiente {en_t}"], "imp"),
        ("ne_pasa", ["Pasa de canción {en_t}", "Salta esta canción {en_t}", "Cambia de canción {en_t}"], "imp"),
        ("ne_otra", ["Otra canción {en_t}", "Esta no me gusta, pasa a la siguiente {en_t}", "Adelante {en_t}"], ""),
    ],
    "previous": [
        ("pr_anterior", ["Canción anterior {en_t}", "Pon la canción anterior {en_t}", "Anterior {en_t}"], "imp"),
        ("pr_vuelve", ["Vuelve a la anterior {en_t}", "Atrás una canción {en_t}", "Pon otra vez la de antes {en_t}"],
         "imp"),
        ("pr_repite", ["Repite la canción anterior {en_t}", "Retrocede una canción {en_t}",
                       "Vuelve atrás {en_t}"], ""),
    ],
    "climate_set": [
        ("cl_pon", ["Pon {t} a {temp} grados", "Pon {t} a {temp}", "Kivi, pon {t} a {temp} grados"], ""),
        ("cl_temp", ["Pon la temperatura {de_t} a {temp}", "Sube la temperatura {de_t} a {temp} grados",
                     "Baja la temperatura {de_t} a {temp} grados"], "imp"),
        ("cl_quiero", ["Quiero {temp} grados {en_t}", "Ajusta {t} a {temp} grados", "Programa {t} a {temp}"], ""),
        ("cl_hace", ["Hace frío, pon {t} a {temp}", "Hace calor, pon {t} a {temp} grados",
                     "¿Puedes poner {t} a {temp} grados?"], ""),
    ],
    "fan_speed": [
        ("fs_pon", ["Pon {t} al {pct}", "Pon {t} al {pct} por ciento", "Deja {t} al {pct} por ciento"], "imp"),
        ("fs_velocidad", ["Velocidad {de_t} al {pct}", "Pon la velocidad {de_t} al {pct} por ciento",
                          "Sube {t} al {pct}"], "imp"),
        ("fs_ajusta", ["Ajusta {t} al {pct}", "Baja {t} al {pct} por ciento", "¿Puedes poner {t} al {pct}?"], ""),
    ],
    "start_timer": [
        ("st_pon", ["Pon un temporizador de {dur}", "Pon un temporizador de {dur}, por favor",
                    "Kivi, pon un temporizador de {dur}"], ""),
        ("st_corto", ["Temporizador de {dur}", "Temporizador {dur}", "Un temporizador de {dur}"], ""),
        ("st_avisa", ["Avísame en {dur}", "Avísame dentro de {dur}", "Cuenta {dur}"], ""),
        ("st_para", ["Pon un temporizador de {dur} para la pasta", "Temporizador de {dur} para el horno",
                     "Ponme un temporizador de {dur} para la lavadora"], ""),
        ("st_programa", ["Programa un temporizador de {dur}", "Crea un temporizador de {dur}",
                         "¿Puedes poner un temporizador de {dur}?"], ""),
    ],
    "cancel_timer": [
        ("ct_cancela", ["Cancela el temporizador", "Cancela el temporizador, por favor", "Kivi, cancela el temporizador"],
         ""),
        ("ct_quita", ["Quita el temporizador", "Borra el temporizador", "Elimina el temporizador"], ""),
        ("ct_para", ["Para el temporizador", "Detén el temporizador", "Olvida el temporizador"], ""),
    ],
}

# Negatives: families of sentences that are not a home command (kind question) or not one this
# decider may execute (kind other). {t} templates use names of the current home (hard negatives).
NEG = {
    "question": [
        ("q_general1", ["¿Cuál es la capital de Portugal?", "¿Cuántos habitantes tiene Madrid?",
                        "¿Quién escribió La Celestina?", "¿En qué año llegó el hombre a la Luna?",
                        "¿Cuánto mide la Torre Eiffel?", "¿Cuál es el río más largo de España?"]),
        ("q_general2", ["¿Cuánto es doce por ocho?", "¿Cuál es la raíz cuadrada de ochenta y uno?",
                        "¿Cuántos gramos tiene un kilo?", "¿Cuántas patas tiene una araña?",
                        "¿Cuántos días tiene un año bisiesto?", "¿Cuánto son veinte euros en dólares?"]),
        ("q_general3", ["¿Por qué el cielo es azul?", "¿Cómo se hace una tortilla de patatas?",
                        "¿Qué significa la palabra efímero?", "¿Cómo se dice ventana en inglés?",
                        "Explícame qué es un agujero negro", "¿Qué diferencia hay entre un virus y una bacteria?"]),
        ("q_general4", ["¿Quién ganó el mundial de fútbol de dos mil diez?", "¿Cuál es el planeta más grande?",
                        "¿Qué animal es el más rápido del mundo?", "¿De qué color es una esmeralda?",
                        "¿Cuántos huesos tiene el cuerpo humano?", "¿Quién pintó el Guernica?"]),
        ("q_chat1", ["Hola, ¿qué tal estás?", "Buenas noches, Kivi", "Gracias", "Muchas gracias, Kivi",
                     "¿Cómo te llamas?", "Buenos días"]),
        ("q_chat2", ["Cuéntame un chiste", "Me aburro", "¿Qué me recomiendas cenar hoy?",
                     "Dime algo bonito", "¿Tú qué opinas del fútbol?", "Cántame una canción"]),
        ("q_chat3", ["Nada, olvídalo", "Da igual", "No era nada", "Vale, perfecto", "Eso es todo",
                     "Déjalo, no importa"]),
        ("q_live", ["¿Qué tiempo va a hacer mañana?", "¿Va a llover esta tarde?", "¿Qué hora es?",
                    "¿Qué día es hoy?", "¿Qué noticias hay hoy?", "¿Cómo ha quedado el partido?"]),
        ("q_state1", ["¿Está {adj:encendid} {t}?", "¿Está {adj:apagad} {t}?", "¿Quién ha encendido {t}?"]),
        ("q_state2", ["¿Está {adj:abiert} {t}?", "¿Qué temperatura hace {en_area}?",
                      "¿Cuánto tiempo lleva {adj:encendid} {t}?"]),
        ("q_state3", ["¿Cuánto falta para el temporizador?", "¿Cuánto le queda al temporizador?",
                      "¿Hay algún temporizador puesto?"]),
        ("q_tv1", ["Te dije que no volvieras a esta casa", "¿Dónde estabas anoche?", "No pienso perdonarte nunca",
                   "Ha sido un placer conocerle, señor", "Corre, que llegamos tarde", "Esto no ha terminado"]),
        ("q_tv2", ["Y ahora, el tiempo para mañana en toda la península", "Volvemos después de la publicidad",
                   "El equipo visitante ha marcado en el último minuto", "Aquí tienen las noticias de las nueve",
                   "Los mercados cierran hoy con ligeras subidas", "Atención a esta oferta por tiempo limitado"]),
        ("q_tv3", ["Le pidió que apagara la luz antes de salir", "Cuando bajó la persiana ya era de noche",
                   "Mi abuela siempre encendía la radio por las mañanas", "Nadie sabía quién había abierto la puerta",
                   "Subió el volumen para no oír la discusión", "La tele estuvo encendida toda la noche"]),
    ],
    "other": [
        ("o_music", ["Pon música relajante", "Pon algo de jazz", "Ponme la radio de los cuarenta",
                     "Busca canciones de los ochenta", "Pon una lista de música para cocinar", "Reproduce música clásica"]),
        ("o_apps", ["Pon Netflix", "Abre la aplicación de YouTube", "Cambia a la uno", "Pon el canal de deportes",
                    "Graba el partido de esta noche", "Busca una película de risa"]),
        ("o_tasks", ["Añade leche a la lista de la compra", "Recuérdame mañana llamar al médico",
                     "Despiértame a las siete", "Pide una pizza", "Llama a mi hermana", "Manda un mensaje a mi madre"]),
        ("o_tasks2", ["Pon una alarma para las ocho", "Apunta que tengo dentista el lunes", "Busca recetas de lentejas",
                      "Reserva mesa para dos", "Envía un correo al trabajo", "Compra pilas"]),
        ("o_missing", ["Enciende {t}", "Apaga {t}", "Pon {t} al {pct} por ciento", "Abre {t}", "Baja {t}",
                       "Silencia {t}"]),
        ("o_vague", ["Haz algo con la luz", "Arregla lo de la tele", "Eso de antes otra vez", "Lo de siempre",
                     "Ponlo como ayer", "Cambia eso"]),
    ],
}

# Which families go to which split (held out by family). Every intent keeps >= 1 family per split.
def split_families(rng, fams, dev_share=0.2, test_share=0.3):
    ids = [f[0] for f in fams]
    rng.shuffle(ids)
    n = len(ids)
    n_test = max(1, round(n * test_share))
    n_dev = max(1, round(n * dev_share))
    if n - n_test - n_dev < 1:
        n_dev = 1
        n_test = max(1, n - 2)
    return {"test": set(ids[:n_test]), "dev": set(ids[n_test:n_test + n_dev]), "train": set(ids[n_test + n_dev:])}


# ---- values -------------------------------------------------------------------------------------

def sample_percent(rng):
    return rng.choice([10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 65, 70, 75, 80, 85, 90, rng.randint(1, 99)])


def sample_temp(rng):
    t = rng.randint(16, 28)
    return t + (0.5 if rng.random() < 0.2 else 0.0)


def sample_duration(rng):
    """(seconds, spoken words, written)"""
    c = rng.random()
    if c < 0.55:
        m = rng.choice([1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 45, 50, rng.randint(2, 59)])
        unit = "minuto" if m == 1 else "minutos"
        sw = ("un" if m == 1 else ne.words_es(m)) + " " + unit
        return m * 60, sw, f"{m} {unit}"
    if c < 0.65:
        s = rng.choice([30, 40, 45, 50, 90])
        return s, f"{ne.words_es(s)} segundos", f"{s} segundos"
    if c < 0.75:
        return 1800, "media hora", "media hora"
    if c < 0.82:
        return 5400, "una hora y media", "una hora y media"
    if c < 0.87:
        return 900, "un cuarto de hora", "un cuarto de hora"
    if c < 0.93:
        h = rng.choice([1, 2])
        return h * 3600, ("una hora" if h == 1 else "dos horas"), ("una hora" if h == 1 else "2 horas")
    m, s = rng.choice([1, 2, 3, 5]), rng.choice([15, 30])
    um = "minuto" if m == 1 else "minutos"
    sw = ("un" if m == 1 else ne.words_es(m)) + f" {um} y {ne.words_es(s)} segundos"
    return m * 60 + s, sw, f"{m} {um} y {s} segundos"


def fmt_pct(v, style):
    return ne.words_es(v) if style == "words" else str(v)


def fmt_temp(v, style):
    if style == "words":
        return ne.words_es(v)
    return (f"{v:.1f}".replace(".", ",") if v != int(v) else str(int(v)))


# ---- rendering ----------------------------------------------------------------------------------

def render(tpl, slots, style):
    def rep(m):
        key = m.group(1)
        if key.startswith("adj:"):
            return agree(key[4:], slots["g"])
        if key == "lo":
            return pron(slots["g"])
        if key == "pct":
            return fmt_pct(slots["value"], style)
        if key == "temp":
            return fmt_temp(slots["value"], style)
        if key == "dur":
            return slots["dur_words"] if style == "words" else slots["dur_written"]
        if key == "t":
            return slots["np"]
        if key in ("de_t", "a_t", "en_t"):
            if not slots["np"]:
                return ""
            return {"de_t": de, "a_t": a_, "en_t": en}[key](slots["np"])
        if key in ("area", "de_area", "en_area"):
            a = slots["area_np"]
            return {"area": a, "de_area": de(a), "en_area": en(a)}[key]
        raise KeyError(key)
    s = re.sub(r"\{([a-z_:]+)\}", rep, tpl)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([?,.!])", r"\1", s)
    s = re.sub(r",\s*([?.!])", r"\1", s)
    return s[0].upper() + s[1:] if s else s


def gen_command(rng, home, intent, tpl, flags, opts):
    """One command item for `home`, or None if the template cannot be filled in this home."""
    slots = {"g": "f", "np": "", "area_np": None}
    target = None
    if intent not in sc.NO_TARGET:
        if "all" in flags:
            cands = [o for o in opts if o.key.startswith("a:") and o.key.endswith(":all")]
            if not cands:
                return None
            o = rng.choice(cands)
            target = o.key
            slots["area_np"] = np_area(home, o.key.split(":")[1])
        else:
            cands = [o for o in opts if sc.compatible(intent, o) and not o.key.endswith(":all")]
            rng.shuffle(cands)
            for o in cands:
                exprs = expressions(home, intent, o, rng)
                if "{t}" not in tpl and not any(p in tpl for p in ("{de_t}", "{en_t}", "{a_t}")):
                    exprs = []
                imp_ok = ("imp" in flags and (flags != "imp_light" or o.key.endswith(":light")))
                exprs = [x for x in exprs if (not x[2] and x[0]) or (x[2] and imp_ok)]
                # implicit lights: "la luz" of the satellite area in imp_light templates only
                if flags == "imp_light" and o.key == f"a:{home['satellite_area']}:light" and rng.random() < 0.3:
                    exprs.append(("", "f", True))
                if exprs:
                    np_, g, _imp = rng.choice(exprs)
                    slots.update(np=np_, g=g)
                    target = o.key
                    break
            if target is None:
                return None
    vkind = sc.INTENTS[intent][4]
    value = None
    if vkind == "percent":
        value = 50 if "mitad" in tpl else (100 if ("máximo" in tpl or "tope" in tpl) else sample_percent(rng))
        slots["value"] = value
    elif vkind == "temperature":
        value = sample_temp(rng)
        slots["value"] = value
    elif vkind == "duration":
        value, slots["dur_words"], slots["dur_written"] = sample_duration(rng)
    spoken = render(tpl, slots, "words")
    written = render(tpl, slots, "digits")
    return dict(kind="command", intent=intent, target=target, value=value, spoken=spoken, written=written)


def missing_target_np(rng, home):
    """A device or room that this home does not have (for 'other' negatives)."""
    names = {e["name"].casefold() for e in home["entities"].values()}
    rooms = {a["name"].casefold() for a in home["areas"].values()}
    for _ in range(50):
        if rng.random() < 0.5:
            d = rng.choice(["light", "cover", "media_player", "switch"])
            pool = [x for sp in DEVICES.values() for x in sp[d]]
            name, g, _ = rng.choice(pool)
            if name.casefold() in names:
                continue
            room, rg, _ = rng.choice([x for sp in ROOMS.values() for x in sp])
            if room.casefold() in rooms:
                return f"{FEM_ART[g]} {lower_name(name)} {de(FEM_ART[rg] + ' ' + lower_name(room))}", g
        else:
            room, rg, _ = rng.choice([x for sp in ROOMS.values() for x in sp])
            if room.casefold() in rooms:
                continue
            return f"la luz {de(FEM_ART[rg] + ' ' + lower_name(room))}", "f"
    return "la luz del garaje", "f"


def gen_negative(rng, home, kind, tpl, opts):
    slots = {"g": "f", "np": "", "area_np": np_area(home, home["satellite_area"])}
    if "{t}" in tpl or "{adj:" in tpl:
        if kind == "other":
            slots["np"], slots["g"] = missing_target_np(rng, home)
        else:
            ents = [o for o in opts if o.key.startswith("e:")]
            o = rng.choice(ents)
            ex = [x for x in expressions(home, "turn_on", o, rng) if x[0]]
            if not ex:
                return None
            slots["np"], slots["g"] = rng.choice(ex)[:2]
    if "{en_area}" in tpl:
        slots["area_np"] = np_area(home, rng.choice(list(home["areas"])))
    slots["value"] = sample_percent(rng)
    return dict(kind=kind, intent=None, target=None, value=None, spoken=render(tpl, slots, "words"),
                written=render(tpl, slots, "digits"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--homes", type=int, nargs=3, default=[60, 12, 16], help="random homes train dev test")
    ap.add_argument("--commands", type=int, nargs=3, default=[5000, 500, 800])
    ap.add_argument("--negatives", type=int, nargs=3, default=[1600, 200, 300])
    a = ap.parse_args()
    rng = random.Random(a.seed)
    fam_split = {i: split_families(rng, fams) for i, fams in T.items()}
    neg_split = {k: split_families(rng, fams) for k, fams in NEG.items()}
    homes = {"train": [], "dev": [], "test": [base_home()]}
    for split, n in zip(("train", "dev", "test"), a.homes):
        for i in range(n):
            homes[split].append(random_home(rng, split, f"{split}{i:03d}"))
    (a.out / "homes").mkdir(parents=True, exist_ok=True)
    for split, hs in homes.items():
        for h in hs:
            inv = {k: v for k, v in h.items() if k not in ("gender", "pool", "id")}
            inv.setdefault("calendar_events", {"today": [], "week": []})
            (a.out / "homes" / f"{h['id']}.yaml").write_text(yaml.safe_dump(inv, allow_unicode=True, sort_keys=False))
            (a.out / "homes" / f"{h['id']}.meta.json").write_text(json.dumps(
                {"gender": h["gender"], "pool": h["pool"]}, ensure_ascii=False))
    split_info = {"families": {i: {s: sorted(v) for s, v in d.items()} for i, d in fam_split.items()},
                  "negatives": {k: {s: sorted(v) for s, v in d.items()} for k, d in neg_split.items()}}
    (a.out / "splits.json").write_text(json.dumps(split_info, indent=1, ensure_ascii=False))
    intents = list(T)
    for si, split in enumerate(("train", "dev", "test")):
        items, seen = [], set()
        tries = 0
        while sum(1 for x in items if x["kind"] == "command") < a.commands[si] and tries < a.commands[si] * 30:
            tries += 1
            h = rng.choice(homes[split])
            opts = sc.target_options(h)
            intent = rng.choice(intents)
            fams = [f for f in T[intent] if f[0] in fam_split[intent][split]]
            fam = rng.choice(fams)
            tpl = rng.choice(fam[1])
            it = gen_command(rng, h, intent, tpl, fam[2], opts)
            if it is None or (h["id"], it["written"]) in seen:
                continue
            seen.add((h["id"], it["written"]))
            items.append(dict(it, home=h["id"], family=fam[0]))
        tries = 0
        while sum(1 for x in items if x["kind"] != "command") < a.negatives[si] and tries < a.negatives[si] * 30:
            tries += 1
            h = rng.choice(homes[split])
            kind = "question" if rng.random() < 0.65 else "other"
            fams = [f for f in NEG[kind] if f[0] in neg_split[kind][split]]
            fam = rng.choice(fams)
            tpl = rng.choice(fam[1])
            it = gen_negative(rng, h, kind, tpl, sc.target_options(h))
            if it is None or (h["id"], it["written"]) in seen:
                continue
            seen.add((h["id"], it["written"]))
            items.append(dict(it, home=h["id"], family=fam[0]))
        rng.shuffle(items)
        with open(a.out / f"items_{split}.jsonl", "w", encoding="utf-8") as fh:
            for i, it in enumerate(items):
                fh.write(json.dumps(dict(id=f"{split}-{i:05d}", split=split, **it), ensure_ascii=False) + "\n")
        print(split, len(items), "items",
              sum(1 for x in items if x["kind"] == "command"), "commands", len(homes[split]), "homes")


if __name__ == "__main__":
    main()
