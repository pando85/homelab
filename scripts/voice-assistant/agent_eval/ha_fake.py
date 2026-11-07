"""Fake Home Assistant for the dry-run evaluation: tool schemas, a target
resolver, plausible tool results, and the system prompt.

Nothing here talks to Home Assistant. Tool names, descriptions and argument
shapes follow Home Assistant 2026.10.4 as read from the source on GitHub
(homeassistant/components/{intent,light,climate,media_player,fan,calendar,
homeassistant,llm}/llm.py and the intent handlers). Items that could not be
confirmed from source are marked "(unconfirmed)" below. Tool results are
modelled on the source, not captured from a live Home Assistant.
"""
import copy
import json
import os
import re

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))

# Device classes accepted by on/off style tools (source: ONOFF_DEVICE_CLASSES in
# components/intent/__init__.py; the enum members listed here are from memory of
# the cover/switch/media_player device-class enums: unconfirmed).
ONOFF_DEVICE_CLASSES = [
    "awning", "blind", "curtain", "damper", "door", "garage", "gate", "shade",
    "shutter", "window", "outlet", "switch", "tv", "speaker", "receiver",
]
MEDIA_DEVICE_CLASSES = ["tv", "speaker", "receiver"]

READ_ONLY_TOOLS = {
    "homeassistant__GetLiveContext", "calendar__get_events", "llm__GetDateTime",
    "intent__HassTimerStatus",
}


def _fn(name, description, props=None, required=None):
    params = {"type": "object", "properties": props or {}}
    if required:
        params["required"] = required
    return {"type": "function", "function": {"name": name, "description": description, "parameters": params}}


def _target_props(domains=None, device_classes=None, name_area_floor=True):
    props = {}
    if name_area_floor:
        props["name"] = {"type": "string"}
        props["area"] = {"type": "string"}
        props["floor"] = {"type": "string"}
    dom = {"type": "string"} if not domains else {"type": "string", "enum": domains}
    props["domain"] = {"type": "array", "items": dom}
    if device_classes:
        props["device_class"] = {"type": "array", "items": {"type": "string", "enum": device_classes}}
    return props


def _amount_props(prefix=""):
    return {
        f"{prefix}hours": {"type": "integer", "minimum": 0},
        f"{prefix}minutes": {"type": "integer", "minimum": 0},
        f"{prefix}seconds": {"type": "integer", "minimum": 0},
    }


def build_tools():
    """OpenAI-format tool list modelled on Home Assistant's Assist LLM API."""
    t = []
    t.append(_fn("intent__HassTurnOn",
                 "Turns on/opens/presses a device or entity. For locks, this performs a 'lock' action. "
                 "Use for requests like 'turn on', 'activate', 'enable', or 'lock'.",
                 _target_props(device_classes=ONOFF_DEVICE_CLASSES)))
    t.append(_fn("intent__HassTurnOff",
                 "Turns off/closes a device or entity. For locks, this performs an 'unlock' action. "
                 "Use for requests like 'turn off', 'deactivate', 'disable', or 'unlock'.",
                 _target_props(device_classes=ONOFF_DEVICE_CLASSES)))
    t.append(_fn("intent__HassSetPosition", "Sets the position of a device or entity",
                 {**_target_props(device_classes=ONOFF_DEVICE_CLASSES[:10]),
                  "position": {"type": "integer", "minimum": 0, "maximum": 100}}, ["position"]))
    t.append(_fn("intent__HassStopMoving", "Stops a moving device or entity",
                 _target_props(device_classes=ONOFF_DEVICE_CLASSES[:10])))
    t.append(_fn("intent__HassCancelAllTimers", "Cancels all timers", {"area": {"type": "string"}}))
    # Timer tools: HA exposes them only when the requesting device supports timers.
    # Whether the webOS satellite will advertise that is not decided (unconfirmed).
    t.append(_fn("intent__HassStartTimer", "Starts a new timer",
                 {**_amount_props(), "name": {"type": "string"}, "conversation_command": {"type": "string"}}))
    t.append(_fn("intent__HassCancelTimer", "Cancels a timer",
                 {**_amount_props("start_"), "name": {"type": "string"}, "area": {"type": "string"}}))
    t.append(_fn("intent__HassIncreaseTimer", "Adds more time to a timer",
                 {**_amount_props(), **_amount_props("start_"), "name": {"type": "string"}, "area": {"type": "string"}}))
    t.append(_fn("intent__HassDecreaseTimer", "Removes time from a timer",
                 {**_amount_props(), **_amount_props("start_"), "name": {"type": "string"}, "area": {"type": "string"}}))
    t.append(_fn("intent__HassPauseTimer", "Pauses a running timer",
                 {**_amount_props("start_"), "name": {"type": "string"}, "area": {"type": "string"}}))
    t.append(_fn("intent__HassUnpauseTimer", "Resumes a paused timer",
                 {**_amount_props("start_"), "name": {"type": "string"}, "area": {"type": "string"}}))
    t.append(_fn("intent__HassTimerStatus", "Reports the current status of timers",
                 {**_amount_props("start_"), "name": {"type": "string"}, "area": {"type": "string"}}))
    t.append(_fn("light__HassLightSet", "Sets the brightness percentage or color of a light",
                 {**_target_props(domains=["light"]),
                  "color": {"type": "string"},
                  "temperature": {"type": "integer", "minimum": 0},
                  "brightness": {"type": "integer", "minimum": 0, "maximum": 100,
                                 "description": "The brightness percentage of the light between 0 and 100, where 0 is off and 100 is fully lit"}}))
    t.append(_fn("climate__HassClimateSetTemperature", "Sets the target temperature of a climate device or entity",
                 {"temperature": {"type": "number"}, "area": {"type": "string"}, "name": {"type": "string"},
                  "floor": {"type": "string"}}, ["temperature"]))
    mp = _target_props(domains=["media_player"], device_classes=MEDIA_DEVICE_CLASSES)
    t.append(_fn("media_player__HassMediaPause", "Pauses a media player", mp))
    t.append(_fn("media_player__HassMediaUnpause", "Resumes a media player", mp))
    t.append(_fn("media_player__HassMediaNext", "Skips a media player to the next item", mp))
    t.append(_fn("media_player__HassMediaPrevious", "Replays the previous item for a media player", mp))
    t.append(_fn("media_player__HassMediaPlayerMute", "Mutes a media player", mp))
    t.append(_fn("media_player__HassMediaPlayerUnmute", "Unmutes a media player", mp))
    t.append(_fn("media_player__HassSetVolume", "Sets the volume percentage of a media player",
                 {**mp, "volume_level": {"type": "integer", "minimum": 0, "maximum": 100,
                                         "description": "The volume percentage of the media player"}},
                 ["volume_level"]))
    t.append(_fn("media_player__HassSetVolumeRelative", "Increases or decreases the volume of a media player",
                 {"volume_step": {"anyOf": [{"type": "string", "enum": ["up", "down"]},
                                            {"type": "integer", "minimum": -100, "maximum": 100}]},
                  "name": {"type": "string"}, "area": {"type": "string"}, "floor": {"type": "string"}},
                 ["volume_step"]))
    t.append(_fn("fan__HassFanSetSpeed", "Sets a fan's speed by percentage",
                 {**_target_props(domains=["fan"]),
                  "percentage": {"type": "integer", "minimum": 0, "maximum": 100,
                                 "description": "The speed percentage of the fan"}}, ["percentage"]))
    t.append(_fn("calendar__get_events",
                 "Get events from a calendar. When asked if something happens, search the whole week. "
                 "Results are RFC 5545 which means 'end' is exclusive.",
                 {"calendar": {"type": "string", "enum": ["Calendario familiar", "calendario"]},
                  "range": {"type": "string", "enum": ["today", "week"]}}, ["calendar", "range"]))
    t.append(_fn("homeassistant__GetLiveContext",
                 "Provides real-time information about the CURRENT state, value, or mode of devices, sensors, "
                 "entities, or areas. Use this tool for: 1. Answering questions about current conditions "
                 "(e.g., 'Is the light on?'). 2. As the first step in conditional actions (e.g., 'If the weather "
                 "is rainy, turn off sprinklers' requires checking the weather first). You may filter for devices "
                 "by name, domain, and area, including combining those filters. Prefer filtering by domain when "
                 "searching for multiple devices of the same type.",
                 {"name": {"type": "string", "description": "Filter entities by name or alias (case-insensitive)."},
                  "domain": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                             "description": "Filter entities by domain (e.g. 'light', 'sensor'). Accepts a single domain or a list."},
                  "area": {"type": "string", "description": "Filter entities by area name or alias (case-insensitive)."}}))
    t.append(_fn("llm__GetDateTime", "Provides the current date and time."))
    return t


# --- system prompt ----------------------------------------------------------

PERSONA_ES = (
    "Eres Kivi, el asistente de voz de esta casa. Tu nombre se escribe «Kivi» y se pronuncia «kivi».\n"
    "Habla siempre en español de España, de forma amable y directa. Tus respuestas se leen en voz alta, "
    "así que usa una o dos frases cortas en texto plano: nada de markdown, listas, asteriscos, emojis, "
    "enlaces ni símbolos como ° o %; di «grados» y «por ciento».\n"
    "Para controlar la casa usa siempre las herramientas, y confirma en pocas palabras solo lo que de verdad "
    "se ha hecho; si una herramienta falla, dilo. Si la orden es ambigua, haz una pregunta corta.\n"
    "Si te piden datos en tiempo real que no puedes consultar con tus herramientas (noticias, bolsa, tráfico, "
    "resultados deportivos, previsiones), di claramente que no puedes consultarlos. No inventes nunca datos.\n"
    "Las preguntas generales respóndelas con tus conocimientos, de forma breve."
)

# Verbatim from homeassistant/components/intent/llm.py and homeassistant/components/homeassistant/llm.py
# (2026.10.4). HA's own prompt is English; only the persona above is ours.
DEVICE_CONTROL_TOOL_USAGE_PROMPT = (
    "When controlling Home Assistant always call the intent tools. "
    "Use intent__HassTurnOn to lock and intent__HassTurnOff to unlock a lock. "
    "When controlling a device, prefer passing just name and domain. "
    "When controlling an area, prefer passing just area name and domain."
)
DYNAMIC_CONTEXT_PROMPT = (
    "You ARE equipped to answer questions about the current state of\n"
    "the home by retrieving live context. This is a primary function. Do not state you lack the\n"
    "functionality if the question requires live data.\n"
    'If the user asks about device existence/type (e.g., "Do I have lights in the bedroom?"): Answer\n'
    "from the static context below.\n"
    "If the user asks about the CURRENT state, value, or mode "
    '(e.g., "Is the lock locked?",\n"Is the fan on?", "What mode is the thermostat in?", '
    '"What is the temperature outside?"):\n'
    "    1.  Recognize this requires live data.\n"
    "    2.  You MUST use the provided tool to retrieve live context. This tool will provide the needed "
    "real-time information (like temperature from the local weather, lock status, etc.).\n"
    "    3.  Use the tool's response** to answer the user accurately "
    '(e.g., "The temperature outside is [value from tool].").\n'
    "For general knowledge questions not about the home: Answer truthfully from internal knowledge.\n"
)


def norm(s):
    """Home Assistant's name normalisation: strip and casefold (no accent folding)."""
    return str(s).strip().casefold()


class FakeHome:
    def __init__(self, inventory_path=None):
        path = inventory_path or os.path.join(HERE, "inventory_es.yaml")
        with open(path, encoding="utf-8") as fh:
            self.inv = yaml.safe_load(fh)
        self.areas = self.inv["areas"]
        self.floors = self.inv["floors"]
        self.entities = self.inv["entities"]
        self.satellite_area = self.inv["satellite_area"]
        self.now = self.inv["now"]

    # -- naming helpers
    def names_of(self, eid):
        e = self.entities[eid]
        return [norm(e["name"])] + [norm(a) for a in e.get("aliases", [])]

    def area_ids_matching(self, name):
        n = norm(name)
        return [aid for aid, a in self.areas.items()
                if n == norm(a["name"]) or n in [norm(x) for x in a.get("aliases", [])]]

    def floor_ids_matching(self, name):
        n = norm(name)
        return [fid for fid, f in self.floors.items() if n == norm(f["name"])]

    def area_of(self, eid):
        return self.entities[eid].get("area")

    def floor_of(self, eid):
        a = self.area_of(eid)
        return self.areas[a]["floor"] if a else None

    # -- prompt
    def system_prompt(self):
        static = []
        for eid, e in self.entities.items():
            names = ", ".join([e["name"]] + e.get("aliases", []))
            info = {"names": names, "domain": eid.split(".")[0]}
            if e.get("area"):
                a = self.areas[e["area"]]
                info["areas"] = ", ".join([a["name"]] + a.get("aliases", []))
            static.append(info)
        area = self.areas[self.satellite_area]
        floor = self.floors[area["floor"]]
        area_prompt = (f"You are in area {area['name']} (floor {floor['name']}) and all generic commands like "
                       "'turn on the lights' should target this area.")
        date, time = self.now[:10], self.now[11:19]
        parts = [
            PERSONA_ES,
            f"Current time is {time}. Today's date is {date}.",
            DEVICE_CONTROL_TOOL_USAGE_PROMPT,
            area_prompt,
            DYNAMIC_CONTEXT_PROMPT + "Static Context: An overview of the areas and the devices in this smart home:\n"
            + yaml.safe_dump(static, allow_unicode=True, sort_keys=False),
        ]
        return "\n".join(parts)

    # -- resolver (approximates HA's intent target matching; unconfirmed in detail)
    def resolve(self, args, domains=None, device_class_ok=None, required_states=None):
        """Return (entity_ids, error_text). `args` are the model's slots."""
        name = args.get("name")
        area = args.get("area")
        floor = args.get("floor")
        dom = args.get("domain")
        if isinstance(dom, str):
            dom = [dom]
        dom = [d for d in (dom or []) if d]
        dcs = args.get("device_class")
        if isinstance(dcs, str):
            dcs = [dcs]
        dcs = [d for d in (dcs or []) if d]
        if not any([name, area, floor, dom, dcs]):
            return [], "Service handler cannot target all devices"
        ids = list(self.entities)
        if domains:
            ids = [i for i in ids if i.split(".")[0] in domains]
        if dom:
            ids = [i for i in ids if i.split(".")[0] in [norm(d) for d in dom]]
            if not ids:
                return [], f"No entities matched the domain (given domain {', '.join(dom)})"
        if dcs:
            ids = [i for i in ids if self.entities[i].get("device_class") in dcs]
            if not ids:
                return [], "No entities matched the device class"
        if area:
            aids = self.area_ids_matching(area)
            if not aids:
                return [], f"The area does not exist (given area {area})"
            ids = [i for i in ids if self.area_of(i) in aids]
            if not ids:
                return [], f"No entities were in the area (given area {area})"
        if floor:
            fids = self.floor_ids_matching(floor)
            if not fids:
                return [], f"The floor does not exist (given floor {floor})"
            ids = [i for i in ids if self.floor_of(i) in fids]
            if not ids:
                return [], f"No entities were on the floor (given floor {floor})"
        if name:
            n = norm(name)
            ids = [i for i in ids if n in self.names_of(i)]
            if not ids:
                return [], f"No entities matched the name (given name {name})"
            if len(ids) > 1:
                pref = [i for i in ids if self.area_of(i) == self.satellite_area]
                if len(pref) == 1:
                    ids = pref
                elif not area:
                    return [], f"Multiple entities share the name (given name {name})"
        if required_states:
            ids = [i for i in ids if self.entities[i].get("state") in required_states]
            if not ids:
                return [], "No entities were in the required state"
        return ids, None

    def _ok(self, ids, meta_args=None):
        res = {"response_type": "action_done",
               "data": {"targets": [], "success": [{"name": self.entities[i]["name"], "type": "entity", "id": i} for i in ids],
                        "failed": []},
               "speech": {}}
        return res, {"ok": True, "targets": list(ids), "args": meta_args or {}}

    @staticmethod
    def _err(text, etype="MatchFailedError"):
        return {"error": etype, "error_text": text}, {"ok": False, "targets": [], "args": {}}

    # -- tool execution (dry run): returns (result_json, meta)
    def execute(self, name, args):
        """meta = {ok, targets, args(derived slots for grading)}. Never touches a real system."""
        if not isinstance(args, dict):
            return self._err("arguments are not a JSON object", "Invalid")
        handler = getattr(self, "_t_" + name.replace("__", "_"), None) if name else None
        if handler is None:
            return {"error": "HomeAssistantError", "error_text": f"Tool {name} not found"}, \
                {"ok": False, "targets": [], "args": {}}
        try:
            return handler(args)
        except (TypeError, ValueError, KeyError) as exc:
            return self._err(f"value error: {exc}", "Invalid")

    # Domains the on/off tools can act on; sensors, weather and calendars are never targets.
    ONOFF_DOMAINS = ["light", "switch", "fan", "cover", "valve", "media_player", "climate", "button"]

    def _onoff(self, args, on):
        ids, err = self.resolve(args, domains=self.ONOFF_DOMAINS)
        if err:
            return self._err(err)
        return self._ok(ids)

    def _t_intent_HassTurnOn(self, a):
        return self._onoff(a, True)

    def _t_intent_HassTurnOff(self, a):
        return self._onoff(a, False)

    def _t_intent_HassSetPosition(self, a):
        pos = int(a["position"]) if "position" in a else None
        if pos is None or not 0 <= pos <= 100:
            return self._err("required key not provided @ data['position']", "Invalid")
        ids, err = self.resolve(a, domains=["cover", "valve"])
        return self._err(err) if err else self._ok(ids, {"position": pos})

    def _t_intent_HassStopMoving(self, a):
        ids, err = self.resolve(a, domains=["cover", "valve"])
        return self._err(err) if err else self._ok(ids)

    def _t_intent_HassCancelAllTimers(self, a):
        return {"response_type": "action_done", "data": {"targets": [], "success": [], "failed": []}, "speech": {}}, \
            {"ok": True, "targets": [], "args": {}}

    def _t_intent_HassStartTimer(self, a):
        parts = {k: a.get(k) for k in ("hours", "minutes", "seconds")}
        if all(v in (None, "") for v in parts.values()):
            return self._err("required key not provided @ data[hours|minutes|seconds]", "Invalid")
        total = (int(parts["hours"] or 0) * 3600 + int(parts["minutes"] or 0) * 60 + int(parts["seconds"] or 0))
        return {"response_type": "action_done", "data": {"targets": [], "success": [], "failed": []}, "speech": {}}, \
            {"ok": True, "targets": [], "args": {"total_seconds": total}}

    def _timer_generic(self, a):
        return {"response_type": "action_done", "data": {"targets": [], "success": [], "failed": []}, "speech": {}}, \
            {"ok": True, "targets": [], "args": {}}

    _t_intent_HassCancelTimer = _t_intent_HassIncreaseTimer = _t_intent_HassDecreaseTimer = _timer_generic
    _t_intent_HassPauseTimer = _t_intent_HassUnpauseTimer = _timer_generic

    def _t_intent_HassTimerStatus(self, a):
        left = int(self.inv.get("timer_status_seconds_left", 300))
        res = {"response_type": "query_answer", "data": {"targets": [], "success": [], "failed": []},
               "speech": {"plain": {"speech": f"{left // 60} minutes, {left % 60} seconds left", "extra_data": None}},
               "timers": [{"name": "", "seconds_left": left, "is_active": True}]}
        return res, {"ok": True, "targets": [], "args": {}}

    def _t_light_HassLightSet(self, a):
        args = {}
        if "brightness" in a and a["brightness"] not in (None, ""):
            b = int(a["brightness"])
            if not 0 <= b <= 100:
                return self._err("value must be at most 100 for dictionary value @ data['brightness']", "Invalid")
            args["brightness"] = b
        ids, err = self.resolve(a, domains=["light"])
        return self._err(err) if err else self._ok(ids, args)

    def _t_climate_HassClimateSetTemperature(self, a):
        if "temperature" not in a:
            return self._err("required key not provided @ data['temperature']", "Invalid")
        temp = float(a["temperature"])
        b = {k: a[k] for k in ("name", "area", "floor") if a.get(k)}
        ids, err = (self.resolve(b, domains=["climate"]) if b else
                    self.resolve({"area": self.areas[self.satellite_area]["name"]}, domains=["climate"]))
        return self._err(err) if err else self._ok(ids, {"temperature": temp})

    def _mp(self, a, states=None, extra=None):
        ids, err = self.resolve(a, domains=["media_player"], required_states=states)
        return self._err(err) if err else self._ok(ids, extra)

    def _t_media_player_HassMediaPause(self, a):
        return self._mp(a, {"playing"})

    def _t_media_player_HassMediaUnpause(self, a):
        return self._mp(a)

    def _t_media_player_HassMediaNext(self, a):
        return self._mp(a, {"playing"})

    def _t_media_player_HassMediaPrevious(self, a):
        return self._mp(a, {"playing"})

    def _t_media_player_HassMediaPlayerMute(self, a):
        return self._mp(a)

    def _t_media_player_HassMediaPlayerUnmute(self, a):
        return self._mp(a)

    def _t_media_player_HassSetVolume(self, a):
        if "volume_level" not in a:
            return self._err("required key not provided @ data['volume_level']", "Invalid")
        lv = int(a["volume_level"])
        if not 0 <= lv <= 100:
            return self._err("value must be at most 100 for dictionary value @ data['volume_level']", "Invalid")
        return self._mp(a, None, {"volume_level": lv})

    def _t_media_player_HassSetVolumeRelative(self, a):
        step = a.get("volume_step")
        if step in ("up", "down"):
            direction = step
        else:
            n = int(step)
            direction = "up" if n > 0 else "down"
        b = {k: a[k] for k in ("name", "area", "floor") if a.get(k)}
        if not b:
            b = {"area": self.areas[self.satellite_area]["name"]}
        ids, err = self.resolve(b, domains=["media_player"])
        return self._err(err) if err else self._ok(ids, {"volume_dir": direction})

    def _t_fan_HassFanSetSpeed(self, a):
        if "percentage" not in a:
            return self._err("required key not provided @ data['percentage']", "Invalid")
        p = int(a["percentage"])
        ids, err = self.resolve(a, domains=["fan"])
        return self._err(err) if err else self._ok(ids, {"percentage": p})

    def _t_calendar_get_events(self, a):
        cal = norm(a.get("calendar", ""))
        if cal not in self.names_of("calendar.familia"):
            return {"error": "Calendar not found"}, {"ok": False, "targets": [], "args": {}}
        rng = a.get("range")
        if rng not in ("today", "week"):
            return self._err("value must be one of ['today', 'week'] @ data['range']", "Invalid")
        events = copy.deepcopy(self.inv["calendar_events"][rng])
        events = [e if "T" in e["start"] else {**e, "all_day": True} for e in events]
        return {"events": events}, {"ok": True, "targets": ["calendar.familia"], "args": {}}

    def _t_homeassistant_GetLiveContext(self, a):
        name, area, dom = a.get("name"), a.get("area"), a.get("domain")
        if isinstance(dom, str):
            dom = [dom]
        ids = [i for i in self.entities if i.split(".")[0] not in ("calendar", "script")]
        if dom:
            doms = [norm(d) for d in dom if str(d).strip()]
            if doms:
                ids = [i for i in ids if i.split(".")[0] in doms]
                if not ids:
                    return {"error": f"No exposed entities found in domain(s): {', '.join(doms)}"}, \
                        {"ok": False, "targets": [], "args": {}}
        if area:
            aids = self.area_ids_matching(area)
            if not aids:
                return {"error": f"Area '{area}' does not exist"}, {"ok": False, "targets": [], "args": {}}
            ids = [i for i in ids if self.area_of(i) in aids]
            if not ids:
                return {"error": f"No exposed entities found in area '{area}'"}, {"ok": False, "targets": [], "args": {}}
        if name:
            ids = [i for i in ids if norm(name) in self.names_of(i)]
            if not ids:
                return {"error": f"No exposed entities matched name '{name}'"}, {"ok": False, "targets": [], "args": {}}
        entities = []
        for i in ids:
            e = self.entities[i]
            info = {"names": ", ".join([e["name"]] + e.get("aliases", [])), "domain": i.split(".")[0],
                    "state": str(e.get("state"))}
            if e.get("area"):
                a_ = self.areas[e["area"]]
                info["areas"] = ", ".join([a_["name"]] + a_.get("aliases", []))
            attrs = {k: (str(v) if isinstance(v, int) else v) for k, v in e.get("attributes", {}).items()}
            if i.startswith("light.") and isinstance(e.get("attributes", {}).get("brightness"), int):
                attrs["brightness_pct"] = str(round(e["attributes"]["brightness"] / 255 * 100))
            if i.startswith("media_player.") and "volume_level" in e.get("attributes", {}):
                pass
            if attrs:
                info["attributes"] = attrs
            entities.append(info)
        text = ("Live Context: An overview of the areas and the devices in this smart home:\n"
                + yaml.safe_dump(entities, allow_unicode=True, sort_keys=False))
        return {"result": text}, {"ok": True, "targets": list(ids), "args": {}}

    def _t_llm_GetDateTime(self, a):
        import datetime
        dt = datetime.datetime.fromisoformat(self.now)
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        return {"date": dt.strftime("%Y-%m-%d"), "time": dt.strftime("%H:%M:%S"), "timezone": "CET",
                "weekday": days[dt.weekday()]}, {"ok": True, "targets": [], "args": {}}


def result_to_content(result):
    """Tool message content: HA hands the model the JSON of the tool result."""
    return json.dumps(result, ensure_ascii=False)
