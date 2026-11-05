"""Pure logic of the decision stage (no model, no network):

  transcript the built-in intents missed
    -> typed questions with options built from the home's inventory
    -> a backend picks one option per question with a probability (decision model or baseline)
    -> our code parses the number, resolves the target entities and maps the decision
       to a Home Assistant intent call, or hands the request off to the LLM agent (Kiais).

Inventory: a dict in the shape of agent_eval/inventory_es.yaml (areas, floors, entities,
satellite_area). Every option list comes from it, so option counts follow the real home.

The decision is gated on its weakest link: the minimum over the questions that were used of the
probability of the chosen option. Incompatible choices (a volume action on a light, a value
command without a number, a target with no entity) always hand off.
"""
import re
import unicodedata

# ---- question texts (Spanish and English variants; the state is always the Spanish transcript) ----

ROUTE = {
    "es": ("¿Qué tipo de petición hace el usuario al asistente de voz de la casa?", {
        "orden": "una orden para controlar un aparato de la casa: luces, persianas, toldo, aire, tele, altavoz, "
                 "ventilador, enchufes",
        "temporizador": "poner, cancelar o consultar un temporizador",
        "estado": "una pregunta sobre cómo está un aparato o qué temperatura hace en una habitación",
        "otra": "cualquier otra cosa: pregunta de cultura general, conversación, noticias, tiempo de mañana, "
                "hora, chiste",
    }),
    "en": ("What kind of request does the user make to the home's voice assistant?", {
        "orden": "a command to control a device in the home: lights, blinds, awning, air conditioning, TV, "
                 "speaker, fan, plugs",
        "temporizador": "start, cancel or check a timer",
        "estado": "a question about the state of a device or the temperature of a room",
        "otra": "anything else: general knowledge question, chat, news, tomorrow's weather, time, a joke",
    }),
}

ACTIONS = {
    "es": ("¿Qué acción pide el usuario?", {
        "encender": "encender, abrir o subir del todo (luz, aparato, persiana, toldo); también pedir más luz",
        "apagar": "apagar, cerrar o bajar del todo, o quitar (luz, aparato, persiana, toldo, tele)",
        "fijar": "poner a un valor con un número: brillo, posición, velocidad, volumen o temperatura",
        "subir_volumen": "subir el volumen, poner más alto",
        "bajar_volumen": "bajar el volumen, poner más bajo",
        "silenciar": "silenciar, quitar el sonido",
        "quitar_silencio": "quitar el silencio, volver a poner el sonido",
        "pausar": "pausar la reproducción, o parar una persiana o un toldo que se mueve",
        "reanudar": "reanudar o continuar la reproducción",
        "siguiente": "pasar a la siguiente canción",
    }),
    "en": ("Which action does the user ask for?", {
        "encender": "turn on, open or raise fully (light, device, blind, awning); also asking for more light",
        "apagar": "turn off, close or lower fully, or remove (light, device, blind, awning, TV)",
        "fijar": "set to a value given as a number: brightness, position, speed, volume or temperature",
        "subir_volumen": "volume up, louder",
        "bajar_volumen": "volume down, quieter",
        "silenciar": "mute",
        "quitar_silencio": "unmute",
        "pausar": "pause playback, or stop a moving blind or awning",
        "reanudar": "resume or continue playback",
        "siguiente": "skip to the next song",
    }),
}

TIMER = {
    "es": ("¿Qué quiere hacer con el temporizador?", {
        "iniciar": "poner o empezar un temporizador nuevo", "cancelar": "cancelar o quitar el temporizador",
        "consultar": "saber cuánto tiempo le queda"}),
    "en": ("What does the user want to do with the timer?", {
        "iniciar": "start a new timer", "cancelar": "cancel the timer", "consultar": "ask how much time is left"}),
}

AREA_Q = {"es": ("¿De qué habitación o zona de la casa habla?", "no nombra ninguna habitación (aquí)",
                 "toda la casa, todas las habitaciones"),
          "en": ("Which room or area of the home is it about?", "no room is named (here)",
                 "the whole home, every room")}
KIND_Q = {"es": "¿Sobre qué tipo de aparato es la orden?", "en": "Which kind of device is the command about?"}
WHICH_Q = {"es": ("¿Qué aparato concreto nombra?", "todos los de ese tipo, o no nombra uno concreto"),
           "en": ("Which specific device is named?", "all of that kind, or no specific one is named")}

# Device kinds the actions can operate. kind -> (domains, cover/media device classes or None, es, en)
KINDS = {
    "luz": ("Luces y lámparas", "Lights and lamps"),
    "persiana": ("Persianas, cortinas, ventanas", "Blinds, curtains, windows"),
    "toldo": ("Toldos", "Awnings"),
    "puerta": ("Puertas de garaje y cancelas", "Garage doors and gates"),
    "clima": ("Aire acondicionado, calefacción, termostato, temperatura", "Air conditioning, heating, thermostat"),
    "tele": ("Televisión, tele", "Television, TV"),
    "altavoz": ("Altavoces, música, reproductores", "Speakers, music, media players"),
    "ventilador": ("Ventiladores", "Fans"),
    "enchufe": ("Enchufes e interruptores de aparatos", "Plugs and switches of appliances"),
    "todo": ("Todo, todos los aparatos de una zona", "Everything, all devices of an area"),
}
ONOFF_KINDS = {"luz", "persiana", "toldo", "puerta", "clima", "tele", "altavoz", "ventilador", "enchufe"}
TODO_KINDS = {"luz", "tele", "altavoz", "ventilador", "enchufe"}   # "apágalo todo": not covers, doors or climate
MEDIA_KINDS = {"tele", "altavoz"}
COVER_KINDS = {"persiana", "toldo", "puerta"}


def kind_of(eid, ent):
    dom = eid.split(".")[0]
    dc = ent.get("device_class") or (ent.get("attributes") or {}).get("device_class")
    if dom == "light":
        return "luz"
    if dom == "cover":
        return {"awning": "toldo", "garage": "puerta", "gate": "puerta", "door": "puerta"}.get(dc, "persiana")
    if dom == "climate":
        return "clima"
    if dom == "media_player":
        return "tele" if dc == "tv" else "altavoz"
    if dom == "fan":
        return "ventilador"
    if dom in ("switch", "input_boolean"):
        return "enchufe"
    return None


def fold(s):
    """Lower case, accents removed, punctuation to spaces."""
    s = unicodedata.normalize("NFKD", str(s).lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"(?<=\d)[,.](?=\d)", "\x00", s)          # keep decimal separators between digits
    s = re.sub(r"[^a-z0-9%\x00]+", " ", s).replace("\x00", ",")
    return re.sub(r"\s+", " ", s).strip()


class Home:
    """Read-only view of an inventory dict for option building and target resolution."""

    def __init__(self, inv):
        self.inv = inv
        self.areas = inv["areas"]
        self.entities = inv["entities"]
        self.satellite_area = inv["satellite_area"]
        self.kind = {eid: kind_of(eid, e) for eid, e in self.entities.items()}

    def kinds_present(self):
        return [k for k in KINDS if k == "todo" or any(v == k for v in self.kind.values())]

    def of_kind(self, kind, area=None):
        if kind == "todo":
            ids = [i for i, k in self.kind.items() if k in TODO_KINDS]
        else:
            ids = [i for i, k in self.kind.items() if k == kind]
        if area is not None:
            ids = [i for i in ids if self.entities[i].get("area") == area]
        return ids

    def label(self, eid, with_area=False):
        e = self.entities[eid]
        names = [e["name"]] + [a for a in e.get("aliases", []) if a]
        s = ", ".join(dict.fromkeys(names))
        if with_area and e.get("area"):
            s += " (" + self.areas[e["area"]]["name"] + ")"
        return s


def area_options(home, lang):
    _, here, house = AREA_Q[lang]
    opts = {"aqui": here, "casa": house}
    for aid, a in home.areas.items():
        opts["a:" + aid] = ", ".join(dict.fromkeys([a["name"]] + [x for x in a.get("aliases", []) if x]))
    return opts


def kind_options(home, lang, max_names=6):
    col = 0 if lang == "es" else 1
    opts = {}
    for k in home.kinds_present():
        desc = KINDS[k][col]
        if k != "todo":
            names = list(dict.fromkeys(home.entities[i]["name"] for i in home.of_kind(k)))
            extra = names[:max_names]
            if extra:
                desc += ": " + ", ".join(extra)
        opts[k] = desc
    return opts


def choice(instr, criteria):
    return {"type": "choice", "instructions": instr, "criteria": dict(criteria)}


def first_pass(home, lang):
    """Questions of the first forward pass: request type, action, timer action, area, device kind."""
    return {"peticion": choice(*ROUTE[lang]), "accion": choice(*ACTIONS[lang]),
            "temporizador": choice(*TIMER[lang]), "zona": choice(AREA_Q[lang][0], area_options(home, lang)),
            "aparato": choice(KIND_Q[lang], kind_options(home, lang))}


def which_question(home, ids, lang, with_area):
    instr, all_text = WHICH_Q[lang]
    opts = {"e:" + i: home.label(i, with_area) for i in ids}
    opts["todos"] = all_text
    return choice(instr, opts)


def split_options(criteria, max_options):
    """Chunks of at most max_options options (a knockout round), each keeping its order."""
    items = list(criteria.items())
    if len(items) <= max_options:
        return [dict(items)]
    n = -(-len(items) // max_options)
    size = -(-len(items) // n)
    return [dict(items[i:i + size]) for i in range(0, len(items), size)]


# ---- numbers ----

UNITS = {"cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
         "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14,
         "quince": 15, "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20,
         "veintiun": 21, "veintiuno": 21, "veintiuna": 21, "veintidos": 22, "veintitres": 23, "veinticuatro": 24,
         "veinticinco": 25, "veintiseis": 26, "veintisiete": 27, "veintiocho": 28, "veintinueve": 29}
TENS = {"treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70, "ochenta": 80,
        "noventa": 90}


def numbers(text):
    """Numbers in a Spanish transcript, in order: digits (22, 21,5, 40%) and words up to 100
    ("cuarenta", "treinta y cinco", "veintiuno y medio", "cien"). Returns [(value, end_index)]."""
    toks = fold(text).replace("%", " % ").split()
    out, i = [], 0
    while i < len(toks):
        t = toks[i]
        val = None
        m = re.fullmatch(r"(\d+)(?:[.,](\d+))?", t.strip(".,"))
        if m:
            val = float(m.group(1) + ("." + m.group(2) if m.group(2) else ""))
            i += 1
        elif t in ("cien", "ciento") and not (i > 0 and toks[i - 1] == "por"):
            val = 100.0
            i += 1
        elif t in TENS:
            val = float(TENS[t])
            i += 1
            if i + 1 < len(toks) and toks[i] == "y" and toks[i + 1] in UNITS and UNITS[toks[i + 1]] < 10:
                val += UNITS[toks[i + 1]]
                i += 2
        elif t in UNITS and not (t in ("un", "una", "uno") and i + 1 < len(toks) and toks[i + 1] not in
                                 ("y", "hora", "horas", "minuto", "minutos", "segundo", "segundos", "%", "por")):
            val = float(UNITS[t])
            i += 1
        else:
            i += 1
            continue
        if i + 1 < len(toks) and toks[i] == "y" and toks[i + 1] in ("medio", "media"):
            val += 0.5
            i += 2
        elif i + 1 < len(toks) and toks[i] == "coma" and toks[i + 1] in UNITS:
            val += UNITS[toks[i + 1]] / 10
            i += 2
        out.append((val, i))
    return out


def value_of(text):
    """The value of a "set" command: the last number, or a word value (mitad 50, máximo 100). None if
    there is none: the command then hands off."""
    f = fold(text)
    nums = numbers(text)
    if nums:
        return nums[-1][0]
    if re.search(r"\b(la mitad|medias?)\b", f):
        return 50.0
    if re.search(r"\b(maximo|tope|todo lo que (da|puede))\b", f):
        return 100.0
    return None


def duration_seconds(text):
    """Timer length from a transcript: "diez minutos" 600, "una hora y media" 5400, "media hora" 1800,
    "90 segundos" 90. None when no duration is found."""
    f = fold(text)
    total, found = 0.0, False
    if re.search(r"\bmedia hora\b", f):
        total += 1800
        found = True
    toks = f.split()
    nums = numbers(text)
    for val, end in nums:
        unit = toks[end] if end < len(toks) else ""
        tail = " ".join(toks[end:end + 3])
        if unit.startswith("hora"):
            total += val * 3600
            found = True
            if tail.startswith(unit + " y media") or tail.startswith(unit + " y medio"):
                total += 1800
        elif unit.startswith("minuto"):
            total += val * 60
            found = True
            if tail.startswith(unit + " y medio"):
                total += 30
        elif unit.startswith("segundo"):
            total += val
            found = True
    return int(total) if found and total > 0 else None


# ---- decision -> call ----

VALUE_TOOL = {"luz": ("light__HassLightSet", "brightness", 0, 100),
              "persiana": ("intent__HassSetPosition", "position", 0, 100),
              "toldo": ("intent__HassSetPosition", "position", 0, 100),
              "ventilador": ("fan__HassFanSetSpeed", "percentage", 0, 100),
              "tele": ("media_player__HassSetVolume", "volume_level", 0, 100),
              "altavoz": ("media_player__HassSetVolume", "volume_level", 0, 100),
              "clima": ("climate__HassClimateSetTemperature", "temperature", 5, 35)}


def resolve_targets(home, zona, kind, which=None):
    """Entity ids for (area option, kind, which option); None when nothing matches (hand off).
    which: None (not asked), "todos", or "e:<entity_id>"."""
    if kind not in KINDS:
        return None
    if which and which.startswith("e:"):
        eid = which[2:]
        return [eid] if eid in home.entities else None
    if zona == "casa":
        ids = home.of_kind(kind)
    elif zona and zona.startswith("a:"):
        ids = home.of_kind(kind, zona[2:])
    else:  # here: this area's devices of the kind; a kind that exists only once elsewhere is that one
        ids = home.of_kind(kind, home.satellite_area)
        if not ids and which is None:
            every = home.of_kind(kind)
            ids = every if len(every) == 1 else []
    return ids or None


def needs_which(home, zona, kind):
    """Candidates for the 'which device' question (with_area flag), or None if the target is already
    determined by area and kind."""
    if kind == "todo" or zona == "casa":
        return None
    if zona and zona.startswith("a:"):
        ids = home.of_kind(kind, zona[2:])
        return (ids, False) if len(ids) > 1 else None
    ids = home.of_kind(kind)
    return (ids, True) if len(ids) > 1 else None


def to_call(home, d, text):
    """Decision dict {peticion, accion, temporizador, zona, aparato, cual} -> (call or None, reason).
    call = {"name", "args", "ok", "targets", "meta_args"} in the grade.py shape."""
    route = d.get("peticion")
    if route == "temporizador":
        t = d.get("temporizador")
        if t == "iniciar":
            secs = duration_seconds(text)
            if not secs:
                return None, "timer without duration"
            return _call("intent__HassStartTimer", {}, [], {"total_seconds": secs}), ""
        if t == "cancelar":
            return _call("intent__HassCancelTimer", {}, [], {}), ""
        if t == "consultar":
            return _call("intent__HassTimerStatus", {}, [], {}), ""
        return None, "timer action unknown"
    if route != "orden":
        return None, "not a command"
    act, kind = d.get("accion"), d.get("aparato")
    ids = resolve_targets(home, d.get("zona"), kind, d.get("cual"))
    if not ids:
        return None, "no target"
    if act in ("encender", "apagar"):
        if kind != "todo" and kind not in ONOFF_KINDS:
            return None, "kind cannot be switched"
        return _call("intent__HassTurnOn" if act == "encender" else "intent__HassTurnOff", {}, ids, {}), ""
    if kind == "todo":
        return None, "only on/off for everything"
    if act == "fijar":
        if kind not in VALUE_TOOL:
            return None, "kind has no value"
        tool, slot, lo, hi = VALUE_TOOL[kind]
        v = value_of(text)
        if v is None or not lo <= v <= hi:
            return None, "no value"
        v = v if slot == "temperature" else int(round(v))
        return _call(tool, {slot: v}, ids, {slot: v}), ""
    if act in ("subir_volumen", "bajar_volumen", "silenciar", "quitar_silencio", "reanudar", "siguiente"):
        if kind not in MEDIA_KINDS:
            return None, "media action on a non-media device"
        tool = {"subir_volumen": "media_player__HassSetVolumeRelative", "bajar_volumen": "media_player__HassSetVolumeRelative",
                "silenciar": "media_player__HassMediaPlayerMute", "quitar_silencio": "media_player__HassMediaPlayerUnmute",
                "reanudar": "media_player__HassMediaUnpause", "siguiente": "media_player__HassMediaNext"}[act]
        meta = {"volume_dir": "up" if act == "subir_volumen" else "down"} if "volumen" in act else {}
        return _call(tool, {}, ids, meta), ""
    if act == "pausar":
        if kind in MEDIA_KINDS:
            return _call("media_player__HassMediaPause", {}, ids, {}), ""
        if kind in COVER_KINDS:
            return _call("intent__HassStopMoving", {}, ids, {}), ""
        return None, "pause on a device that cannot pause"
    return None, "unknown action"


def _call(name, args, ids, meta):
    return {"name": name, "args": dict(args), "ok": True, "targets": list(ids), "meta_args": dict(meta)}


def used_questions(d):
    """Question ids whose answer the call depends on (for the confidence gate)."""
    if d.get("peticion") == "temporizador":
        return ["peticion", "temporizador"]
    if d.get("peticion") != "orden":
        return ["peticion"]
    q = ["peticion", "accion", "aparato", "zona"]
    if d.get("cual") is not None:
        q.append("cual")
    return q


def gate_confidence(d, conf):
    """Minimum confidence over the questions the decision used."""
    return min(conf.get(q, 0.0) for q in used_questions(d))


# ---- grading of a stage outcome ----

def outcome(call, gold_kind, grade_fn):
    """'handoff' | 'rescued' | 'wrong'. gold_kind 'act' (graded with grade_fn(call) -> bool) or 'noact'
    (any executed call is a wrong action: general questions, state questions, unknown targets)."""
    if call is None:
        return "handoff"
    if gold_kind == "noact":
        return "wrong"
    return "rescued" if grade_fn(call) else "wrong"


def mcnemar_p(b, c):
    """Exact two-sided McNemar p-value for b and c discordant pairs."""
    import math
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


# ---- orchestration over any backend ----

def _expand(questions, max_options):
    """Split questions with too many options into knockout chunks "<qid>#<n>"."""
    out, chunked = {}, {}
    for qid, q in questions.items():
        parts = split_options(q["criteria"], max_options)
        if len(parts) == 1:
            out[qid] = q
        else:
            chunked[qid] = []
            for n, crit in enumerate(parts):
                out[f"{qid}#{n}"] = dict(q, criteria=crit)
                chunked[qid].append(f"{qid}#{n}")
    return out, chunked


def _ask_all(ask, text, questions, max_options, stats):
    """Ask every question (knockout rounds for long option lists). Returns {qid: (choice, prob)}."""
    exp, chunked = _expand(questions, max_options)
    res = ask(text, exp)
    stats["calls"] += 1
    out = {}
    for qid in questions:
        if qid not in chunked:
            p = res[qid]
            best = max(p, key=p.get)
            out[qid] = (best, p[best])
    if chunked:
        final, carry = {}, {}
        for qid, parts in chunked.items():
            winners = {}
            for part in parts:
                p = res[part]
                best = max(p, key=p.get)
                winners[best] = p[best]
            final[qid] = dict(questions[qid], criteria={k: questions[qid]["criteria"][k] for k in winners})
            carry[qid] = winners
        sub = _ask_all(ask, text, final, max_options, stats)
        for qid, (best, prob) in sub.items():
            out[qid] = (best, prob * carry[qid][best])
    return out


def decide(ask, home, text, lang="es", max_options=10):
    """Run the decision stage on one transcript.
    ask(text, questions) -> {qid: {option: probability}} (one batched forward pass per call).
    Returns {"decision", "confidence", "gate", "call", "reason", "calls"}."""
    stats = {"calls": 0}
    ans = _ask_all(ask, text, first_pass(home, lang), max_options, stats)
    d = {q: c for q, (c, _) in ans.items()}
    conf = {q: p for q, (_, p) in ans.items()}
    d["cual"] = None
    if d["peticion"] == "orden":
        w = needs_which(home, d["zona"], d["aparato"])
        if w:
            ids, with_area = w
            a2 = _ask_all(ask, text, {"cual": which_question(home, ids, lang, with_area)}, max_options, stats)
            d["cual"], conf["cual"] = a2["cual"]
    call, reason = to_call(home, d, text)
    return {"decision": d, "confidence": conf, "gate": gate_confidence(d, conf), "call": call, "reason": reason,
            "calls": stats["calls"]}


def gated(result, threshold):
    """The call if the decision clears the threshold, else None (hand off)."""
    return result["call"] if result["call"] is not None and result["gate"] >= threshold else None
