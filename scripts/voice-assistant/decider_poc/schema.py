"""Label space of the Kivi decider PoC: kinds, intents, target options built from the CURRENT
home inventory, the mapping of a decision to a Home Assistant intent call, the canonical action
used for scoring, and the confidence gate.

A home is a dict in the shape of agent_eval/inventory_es.yaml (areas, floors, entities,
satellite_area). Nothing here talks to Home Assistant. Pure functions; pyyaml is not needed.
"""
from dataclasses import dataclass

KINDS = {
    "command": "una orden para controlar un dispositivo de esta casa o un temporizador",
    "question": "una pregunta, una charla o una frase de la tele, no una orden de la casa",
    "other": "una orden que no es de la casa, un dispositivo que no existe o algo que no se entiende",
}

# label -> (Home Assistant intent, Spanish description, target domains, group kinds, value kind)
# Group kinds: "light" = all lights of an area / the house, "cover" = all covers of an area,
# "all" = every on/off device of an area.
INTENTS = {
    "turn_on": ("HassTurnOn", "encender una luz, un aparato o la tele", ("light", "switch", "fan", "media_player",
                                                                         "climate"), ("light",), None),
    "turn_off": ("HassTurnOff", "apagar una luz, un aparato o la tele", ("light", "switch", "fan", "media_player",
                                                                          "climate"), ("light", "all"), None),
    "open_cover": ("HassTurnOn", "abrir o subir una persiana o un toldo", ("cover",), ("cover",), None),
    "close_cover": ("HassTurnOff", "cerrar o bajar una persiana o un toldo", ("cover",), ("cover",), None),
    "cover_position": ("HassSetPosition", "poner una persiana o un toldo a una posición", ("cover",), ("cover",),
                       "percent"),
    "stop_cover": ("HassStopMoving", "parar una persiana o un toldo", ("cover",), ("cover",), None),
    "light_set": ("HassLightSet", "cambiar el brillo de una luz a un porcentaje", ("light",), ("light",), "percent"),
    "set_volume": ("HassSetVolume", "poner el volumen a un número", ("media_player",), (), "percent"),
    "volume_up": ("HassSetVolumeRelative", "subir el volumen", ("media_player",), (), None),
    "volume_down": ("HassSetVolumeRelative", "bajar el volumen", ("media_player",), (), None),
    "mute": ("HassMediaPlayerMute", "silenciar", ("media_player",), (), None),
    "unmute": ("HassMediaPlayerUnmute", "quitar el silencio", ("media_player",), (), None),
    "pause": ("HassMediaPause", "pausar lo que suena o se ve", ("media_player",), (), None),
    "resume": ("HassMediaUnpause", "reanudar o seguir reproduciendo", ("media_player",), (), None),
    "next": ("HassMediaNext", "pasar a la siguiente canción o capítulo", ("media_player",), (), None),
    "previous": ("HassMediaPrevious", "volver a la canción o capítulo anterior", ("media_player",), (), None),
    "climate_set": ("HassClimateSetTemperature", "poner el aire o la calefacción a una temperatura", ("climate",), (),
                    "temperature"),
    "fan_speed": ("HassFanSetSpeed", "poner la velocidad de un ventilador", ("fan",), (), "percent"),
    "start_timer": ("HassStartTimer", "poner un temporizador", (), (), "duration"),
    "cancel_timer": ("HassCancelAllTimers", "cancelar o quitar el temporizador", (), (), None),
}
NO_TARGET = {k for k, v in INTENTS.items() if not v[2]}
DOMAIN_WORD = {"light": "luz", "switch": "enchufe", "fan": "ventilador", "media_player": "reproductor",
               "climate": "climatización", "cover": "persiana o toldo"}
GROUP_WORD = {"light": "todas las luces", "cover": "todas las persianas", "all": "todo lo que se apaga"}
ONOFF = ("light", "switch", "fan", "media_player", "climate", "cover")


def domain(eid):
    return eid.split(".", 1)[0]


@dataclass(frozen=True)
class Option:
    key: str          # "e:<entity_id>", "a:<area_id>:<group>", "h:light"
    label: str        # short unique label shown to the model
    text: str         # description shown to the model
    entities: frozenset


def target_options(home):
    """Every target the decider may pick for this home, in a stable order:
    entities of controllable domains, then per-area groups, then house-wide lights."""
    ents, areas = home["entities"], home["areas"]
    opts = []
    for eid, e in ents.items():
        d = domain(eid)
        if d not in ONOFF:
            continue
        area = areas[e["area"]]["name"] if e.get("area") else "sin zona"
        aliases = e.get("aliases") or []
        text = f"{DOMAIN_WORD[d]} en {area}" + (f"; también «{'», «'.join(aliases)}»" if aliases else "")
        opts.append(Option(f"e:{eid}", f"{e['name']} | {area}", text, frozenset([eid])))
    for aid, a in areas.items():
        for g in ("light", "cover", "all"):
            members = frozenset(eid for eid, e in ents.items() if e.get("area") == aid and
                                (domain(eid) == g if g != "all" else domain(eid) in ONOFF))
            if members:
                opts.append(Option(f"a:{aid}:{g}", f"{GROUP_WORD[g]} | {a['name']}", f"{GROUP_WORD[g]} de {a['name']}",
                                   members))
    lights = frozenset(eid for eid in ents if domain(eid) == "light")
    if lights:
        opts.append(Option("h:light", "todas las luces | toda la casa", "todas las luces de la casa", lights))
    return opts


def compatible(intent, option):
    """Can this intent act on this target option?"""
    _ha, _desc, doms, groups, _v = INTENTS[intent]
    k = option.key
    if k.startswith("e:"):
        return domain(k[2:]) in doms
    g = k.rsplit(":", 1)[1]
    if k.startswith("h:") and intent == "turn_off" and g == "light":
        return True
    return g in groups


@dataclass(frozen=True)
class Action:
    """Canonical action for scoring: HA intent, resolved entity set, value."""
    intent: str
    entities: frozenset
    value: object

    def describe(self):
        return f"{self.intent} {sorted(self.entities)} {self.value}"


def canonical(intent, entities, value):
    """Normalise equivalent calls: cover position 0/100 equals close/open, HassTurnOn/Off on covers
    stay as they are; volume relative carries its direction as value."""
    ents = frozenset(entities or [])
    if intent in ("turn_on", "turn_off") and ents and all(domain(e) == "cover" for e in ents):
        intent = "open_cover" if intent == "turn_on" else "close_cover"
    if intent == "cover_position" and value in (0, 100):
        intent, value = ("close_cover" if value == 0 else "open_cover"), None
    if intent in ("volume_up", "volume_down"):
        value = "up" if intent == "volume_up" else "down"
    if isinstance(value, float) and value == int(value) and intent != "climate_set":
        value = int(value)
    ha = INTENTS[intent][0]
    if intent in ("open_cover", "close_cover"):
        ha = intent  # keep open/close distinct from turn_on/turn_off on other domains
    return Action(ha, ents, value)


@dataclass
class Gate:
    """Confidence thresholds; any probability below its threshold hands the request to Kiais."""
    kind: float = 0.9
    intent: float = 0.9
    target: float = 0.9


@dataclass
class Decision:
    act: bool
    reason: str
    intent: str = None
    target: str = None
    value: object = None
    action: Action = None


def decide(p_kind, p_intent, p_target, options, value_parser, text, gate):
    """Combine the three answers into execute-or-hand-off.

    p_kind: {kind: prob}; p_intent: {intent: prob}; p_target: {option key: prob} over `options`.
    The target distribution is restricted to the options compatible with the chosen intent and
    renormalised. value_parser(kind, text) reads the numeric slot. Returns a Decision.
    """
    k = max(p_kind, key=p_kind.get)
    if k != "command" or p_kind[k] < gate.kind:
        return Decision(False, f"kind {k} {p_kind[k]:.2f}")
    it = max(p_intent, key=p_intent.get)
    if p_intent[it] < gate.intent:
        return Decision(False, f"intent {it} {p_intent[it]:.2f}", intent=it)
    vkind = INTENTS[it][4]
    value = value_parser(vkind, text) if vkind else None
    if vkind and value is None:
        return Decision(False, f"no {vkind} value", intent=it)
    if it in NO_TARGET:
        return Decision(True, "ok", intent=it, value=value, action=canonical(it, (), value))
    by_key = {o.key: o for o in options}
    comp = {key: p for key, p in p_target.items() if key in by_key and compatible(it, by_key[key])}
    z = sum(comp.values())
    if not comp or z <= 0:
        return Decision(False, "no compatible target", intent=it)
    best = max(comp, key=comp.get)
    pt = comp[best] / z
    # The raw (unrestricted) mass on compatible targets must also be high: a low mass means the
    # model preferred an incompatible target, i.e. intent and target disagree.
    if pt < gate.target or z < gate.target:
        return Decision(False, f"target {best} {pt:.2f} (mass {z:.2f})", intent=it, target=best)
    return Decision(True, "ok", intent=it, target=best, value=value,
                    action=canonical(it, by_key[best].entities, value))


def ha_call(intent, option, value, home):
    """The Home Assistant intent call (name, slots) the decision maps to; for the P4 sketch and
    for grading against agent_eval's fake home. Entity targets use name + area, groups use area +
    domain, like Home Assistant's own sentence matches."""
    ha = INTENTS[intent][0]
    slots = {}
    if option is not None:
        k = option.key
        if k.startswith("e:"):
            e = home["entities"][k[2:]]
            slots["name"] = e["name"]
            if e.get("area"):
                slots["area"] = home["areas"][e["area"]]["name"]
        else:
            g = k.rsplit(":", 1)[1]
            if k.startswith("a:"):
                slots["area"] = home["areas"][k.split(":")[1]]["name"]
            if g in ("light", "cover"):
                slots["domain"] = [g]
    if intent == "cover_position":
        slots["position"] = value
    elif intent == "light_set":
        slots["brightness"] = value
    elif intent == "set_volume":
        slots["volume_level"] = value
    elif intent in ("volume_up", "volume_down"):
        slots["volume_step"] = "up" if intent == "volume_up" else "down"
    elif intent == "climate_set":
        slots["temperature"] = value
    elif intent == "fan_speed":
        slots["percentage"] = value
    elif intent == "start_timer" and value:
        h, rest = divmod(int(value), 3600)
        m, s = divmod(rest, 60)
        slots.update({k: v for k, v in (("hours", h), ("minutes", m), ("seconds", s)) if v})
    return ha, slots
