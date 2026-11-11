#!/usr/bin/env python3
"""Offline logic test of script.tv_abrir_app and the TvOpenApp intent_script speech.

A small interpreter runs the script's variables / if / stop / action steps over a fake household
with plain Jinja2 (sandboxed) and fakes of the few Home Assistant template functions the files use
(area_entities, area_name, device_id, device_entities, state_attr, states, is_state) and the `match`
test. It is NOT Home Assistant: template syntax that Home Assistant's own environment treats
differently (native-type parsing is approximated) still needs the first real run. Needs jinja2 and
pyyaml:
  python3 -m venv /tmp/v && /tmp/v/bin/pip install jinja2 pyyaml
  /tmp/v/bin/python -m unittest discover -s scripts/voice-assistant/tv_open_app -p 'test_tv_open_app_script.py'
"""
import ast
import os
import re
import unittest

import yaml
from jinja2.sandbox import ImmutableSandboxedEnvironment

HA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "apps", "home-assistant", "config")
OPTIONS_B = ["Jellyfin", "Subsonic Player", "YouTube AdFree", "Movistar Plus+", "Spotify – Música y podcasts",
             "RTVE.es Play", "Prime Video", "Navegador web", "Other"]
OPTIONS_A = ["Netflix", "Prime Video", "Disney+", "Apple TV", "YouTube", "Navegador web", "Other"]


class Stop(Exception):
    def __init__(self, response):
        self.response = response


class World:
    def __init__(self, mp_state_b="off", turn_on_works=True):
        # entity -> (state, attrs); registry reduced to what the templates read.
        self.states = {
            "select.c3_b_tv_app": ("unknown", {"options": OPTIONS_B}),
            "media_player.living_room_c3_b_tv": (mp_state_b, {}),
            "media_player.c3_b_satellite": ("idle", {}),
            "select.c3_a_tv_app": ("YouTube", {"options": OPTIONS_A}),
            "media_player.office_a_c3_a_tv": ("on", {}),
            "media_player.c3_a_satellite": ("idle", {}),
        }
        self.device = {"select.c3_b_tv_app": "dev_b", "select.c3_a_tv_app": "dev_a"}
        self.device_ents = {"dev_b": ["select.c3_b_tv_app", "media_player.living_room_c3_b_tv",
                                      "media_player.c3_b_satellite"],
                            "dev_a": ["select.c3_a_tv_app", "media_player.office_a_c3_a_tv",
                                      "media_player.c3_a_satellite"]}
        self.areas = {"living_room": ("Living room", ["salón"], "dev_b"),
                      "office_a": ("Office A", ["oficina grande", "oficina A", "Casa Norte"], "dev_a")}
        self.turn_on_works = turn_on_works
        self.calls = []

    def area_id_of(self, ref):
        for aid, (name, aliases, _dev) in self.areas.items():
            if ref == aid or ref.lower() == name.lower() or ref.lower() in [a.lower() for a in aliases]:
                return aid
        return None

    def functions(self):
        def area_entities(ref):
            aid = self.area_id_of(ref)
            return list(self.device_ents[self.areas[aid][2]]) if aid else []

        def area_name(entity):
            for name, _a, dev in self.areas.values():
                if entity in self.device_ents[dev]:
                    return name
            return None

        return {
            "area_entities": area_entities,
            "area_name": area_name,
            "device_id": lambda e: self.device.get(e),
            "device_entities": lambda d: self.device_ents.get(d, []),
            "state_attr": lambda e, a: self.states.get(e, (None, {}))[1].get(a),
            "states": lambda e: self.states.get(e, ("unknown", {}))[0],
            "is_state": lambda e, s: self.states.get(e, (None, {}))[0] == s,
        }


ENV = ImmutableSandboxedEnvironment()
ENV.tests["match"] = lambda value, pattern: re.match(pattern, str(value)) is not None


def parse(text):
    text = text.strip()
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text
    return value if isinstance(value, (list, dict, int, float, bool)) else text


def render(value, ctx):
    if isinstance(value, str):
        return parse(ENV.from_string(value).render(**ctx)) if ("{{" in value or "{%" in value) else value
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    return value


def run_steps(steps, ctx, world):
    for step in steps:
        if "variables" in step:
            for key, val in step["variables"].items():
                ctx[key] = render(val, ctx)
        elif "if" in step:
            if render(step["if"], ctx) is True or str(render(step["if"], ctx)).lower() == "true":
                run_steps(step["then"], ctx, world)
        elif "stop" in step:
            raise Stop(ctx[step["response_variable"]])
        elif "action" in step:
            data = render(step.get("data", {}), ctx)
            target = render(step.get("target", {}), ctx)
            world.calls.append((step["action"], target.get("entity_id"), data))
            if step["action"] == "media_player.turn_on" and world.turn_on_works:
                world.states[target["entity_id"]] = ("on", {})
        elif "wait_template" in step or "delay" in step:
            pass  # the fake world answers at once; the timeout itself is Home Assistant's
        else:
            raise AssertionError(f"unknown step {step}")


def run_script(world, **fields):
    with open(os.path.join(HA, "script", "tv_abrir_app.yaml"), encoding="utf-8") as f:
        script = yaml.safe_load(f)["tv_abrir_app"]
    ctx = dict(world.functions())
    ctx.update(fields)
    try:
        run_steps(script["sequence"], ctx, world)
    except Stop as stop:
        return stop.response
    raise AssertionError("script ended without stop")


def speech(response, **slots):
    with open(os.path.join(HA, "integrations", "tv_open_app.yaml"), encoding="utf-8") as f:
        text = yaml.safe_load(f)["intent_script"]["TvOpenApp"]["speech"]["text"]
    ctx = dict(slots)
    if response is not None:
        ctx["action_response"] = response
    return ENV.from_string(text).render(**ctx).strip()


class TvAbrirApp(unittest.TestCase):
    def test_spoken_area_wins_over_satellite_area(self):
        w = World(mp_state_b="on")
        r = run_script(w, app="Netflix", tv="oficina grande", area_id="living_room")
        self.assertEqual((r["status"], r["app"]), ("ok", "Netflix"))
        self.assertEqual(w.calls[-1][:2], ("select.select_option", "select.c3_a_tv_app"))

    def test_satellite_area_used_without_tv(self):
        w = World(mp_state_b="on")
        r = run_script(w, app="prime video", tv="", area_id="living_room")
        self.assertEqual((r["status"], r["app"]), ("ok", "Prime Video"))
        self.assertEqual(w.calls[-1][2], {"option": "Prime Video"})

    def test_case_insensitive_and_unique_prefix(self):
        w = World(mp_state_b="on")
        self.assertEqual(run_script(w, app="YOUTUBE", tv="salón")["app"], "YouTube AdFree")
        self.assertEqual(run_script(w, app="youtube", tv="Casa Norte")["app"], "YouTube")

    def test_ask_tv_without_any_area(self):
        w = World()
        self.assertEqual(run_script(w, app="YouTube")["status"], "ask_tv")
        self.assertEqual(run_script(w, app="YouTube", tv="", area_id="")["status"], "ask_tv")
        self.assertEqual(w.calls, [])

    def test_no_tv_and_no_app(self):
        w = World(mp_state_b="on")
        self.assertEqual(run_script(w, app="YouTube", tv="cocina")["status"], "no_tv")
        self.assertEqual(run_script(w, app="Netflix", tv="salón")["status"], "no_app")
        self.assertEqual(w.calls, [])

    def test_other_is_never_selected(self):
        w = World(mp_state_b="on")
        self.assertEqual(run_script(w, app="Other", tv="salón")["status"], "no_app")
        self.assertEqual(run_script(w, app="oth", tv="salón")["status"], "no_app")
        self.assertEqual(w.calls, [])

    def test_ambiguous_prefix_is_not_guessed(self):
        w = World(mp_state_b="on")
        w.states["select.c3_b_tv_app"] = ("unknown", {"options": ["YouTube AdFree", "YouTube Kids", "Other"]})
        self.assertEqual(run_script(w, app="youtube", tv="salón")["status"], "no_app")

    def test_tv_off_is_turned_on_first(self):
        w = World(mp_state_b="off")
        r = run_script(w, app="Prime Video", tv="salón")
        self.assertEqual(r["status"], "ok")
        self.assertEqual([c[0] for c in w.calls], ["media_player.turn_on", "select.select_option"])
        self.assertEqual(w.calls[0][1], "media_player.living_room_c3_b_tv")  # the _tv player, not the satellite's

    def test_tv_that_stays_off_reports_tv_off(self):
        w = World(mp_state_b="off", turn_on_works=False)
        r = run_script(w, app="Prime Video", tv="salón")
        self.assertEqual(r["status"], "tv_off")
        self.assertEqual([c[0] for c in w.calls], ["media_player.turn_on"])

    def test_unavailable_select(self):
        w = World(mp_state_b="on")
        w.states["select.c3_b_tv_app"] = ("unavailable", {"options": OPTIONS_B})
        self.assertEqual(run_script(w, app="Prime Video", tv="salón")["status"], "tv_unavailable")

    def test_area_id_is_accepted_as_tv(self):
        self.assertEqual(run_script(World(mp_state_b="on"), app="Apple TV", tv="office_a")["status"], "ok")


class Speech(unittest.TestCase):
    def test_sentences(self):
        r = lambda status: {"status": status}  # noqa: E731
        self.assertEqual(speech(r("ok"), app="YouTube", area="salón"), "Abriendo YouTube en la tele del salón.")
        self.assertEqual(speech(r("ok"), app="YouTube", area="oficina grande"),
                         "Abriendo YouTube en la tele de la oficina grande.")
        self.assertEqual(speech(r("ok"), app="YouTube", area="Casa Norte"), "Abriendo YouTube en la tele de Casa Norte.")
        self.assertEqual(speech(r("ok"), app="YouTube"), "Abriendo YouTube.")
        self.assertEqual(speech(r("no_app"), app="Netflix", area="salón"), "No encuentro Netflix en esa tele.")
        self.assertEqual(speech(r("no_tv"), app="Netflix", area="cocina"), "No hay tele en la cocina.")
        self.assertEqual(speech(r("no_tv"), app="Netflix"), "No hay tele en esa habitación.")
        self.assertEqual(speech(r("ask_tv"), app="Netflix"),
                         "¿En qué tele? Por ejemplo: abre Netflix en la tele del salón.")
        self.assertEqual(speech(r("tv_off"), app="Netflix"), "No he podido encender la tele.")
        self.assertEqual(speech(r("tv_unavailable"), app="Netflix"), "La tele no está disponible.")
        self.assertEqual(speech(None, app="Netflix"), "No he podido abrir Netflix.")


if __name__ == "__main__":
    unittest.main()
