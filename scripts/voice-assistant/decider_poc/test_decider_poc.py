"""Self-tests of scripts/voice-assistant/decider_poc (pure functions: number parsing, label
building, gating, miss categories, paraphrase validation). No model, no server, no audio.

Run: python3 -m unittest discover -s scripts/voice-assistant/decider_poc -p 'test_decider_poc.py'
(needs pyyaml; the keyword tests also need rapidfuzz and the webos-voice-satellite checkout and
are skipped without them)
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import numbers_es as ne  # noqa: E402
import schema as sc  # noqa: E402

HOME = {
    "satellite_area": "salon",
    "areas": {"salon": {"name": "Salón", "aliases": []}, "cocina": {"name": "Cocina", "aliases": []}},
    "entities": {
        "light.salon_techo": {"name": "Luz del techo", "area": "salon"},
        "light.salon_pie": {"name": "Lámpara de pie", "area": "salon", "aliases": ["lámpara"]},
        "light.cocina_techo": {"name": "Luz del techo", "area": "cocina"},
        "cover.salon_persiana": {"name": "Persiana", "area": "salon"},
        "media_player.tele": {"name": "Tele", "area": "salon"},
        "sensor.salon_temp": {"name": "Temperatura", "area": "salon"},
    },
}


class NumbersTest(unittest.TestCase):
    def test_words(self):
        self.assertEqual(ne.words_es(21), "veintiuno")
        self.assertEqual(ne.words_es(45), "cuarenta y cinco")
        self.assertEqual(ne.words_es(100), "cien")
        self.assertEqual(ne.words_es(21.5), "veintiuno y medio")

    def test_parse_number(self):
        self.assertEqual(ne.parse_number("pon la luz al cuarenta y cinco"), 45)
        self.assertEqual(ne.parse_number("Pon el aire a 21,5 grados."), 21.5)
        self.assertEqual(ne.parse_number("a veintiuno y medio"), 21.5)
        self.assertEqual(ne.parse_number("ciento veinte"), 120)
        self.assertIsNone(ne.parse_number("pon un temporizador"))  # "un" is an article here

    def test_percent(self):
        self.assertEqual(ne.parse_percent("Pon la luz del salón al 40%"), 40)
        self.assertEqual(ne.parse_percent("al cuarenta por ciento"), 40)
        self.assertEqual(ne.parse_percent("Pon la luz al máximo"), 100)
        self.assertEqual(ne.parse_percent("Deja la persiana a la mitad"), 50)
        self.assertIsNone(ne.parse_percent("al 21,5"))
        self.assertIsNone(ne.parse_percent("Enciende la luz"))

    def test_temperature(self):
        self.assertEqual(ne.parse_temperature("Pon el aire a 22 grados"), 22.0)
        self.assertEqual(ne.parse_temperature("a veintiuno y medio"), 21.5)
        self.assertIsNone(ne.parse_temperature("a 90 grados"))

    def test_duration(self):
        cases = {"Pon un temporizador de diez minutos": 600, "una hora y media": 5400, "media hora": 1800,
                 "un cuarto de hora": 900, "hora y cuarto": 4500, "2 minutos y 30 segundos": 150,
                 "un minuto y medio": 90, "5 min": 300, "noventa segundos": 90, "dos horas": 7200}
        for text, secs in cases.items():
            self.assertEqual(ne.parse_duration(text), secs, text)
        self.assertIsNone(ne.parse_duration("Cancela el temporizador"))

    def test_dispatch(self):
        self.assertIsNone(ne.parse_value(None, "al 40"))
        self.assertEqual(ne.parse_value("percent", "al 40"), 40)


class LabelTest(unittest.TestCase):
    def test_target_options(self):
        keys = [o.key for o in sc.target_options(HOME)]
        self.assertIn("e:light.salon_pie", keys)
        self.assertNotIn("e:sensor.salon_temp", keys)  # sensors are never targets
        self.assertIn("a:salon:light", keys)
        self.assertIn("a:salon:all", keys)
        self.assertIn("h:light", keys)
        self.assertEqual(len(keys), len(set(keys)))
        labels = [o.label for o in sc.target_options(HOME)]
        self.assertEqual(len(labels), len(set(labels)))  # duplicate names stay distinguishable
        grp = {o.key: o for o in sc.target_options(HOME)}["a:salon:light"]
        self.assertEqual(grp.entities, frozenset({"light.salon_techo", "light.salon_pie"}))

    def test_compatible(self):
        opts = {o.key: o for o in sc.target_options(HOME)}
        self.assertTrue(sc.compatible("turn_on", opts["e:light.salon_pie"]))
        self.assertFalse(sc.compatible("open_cover", opts["e:light.salon_pie"]))
        self.assertTrue(sc.compatible("open_cover", opts["e:cover.salon_persiana"]))
        self.assertTrue(sc.compatible("turn_off", opts["a:salon:all"]))
        self.assertFalse(sc.compatible("turn_on", opts["a:salon:all"]))
        self.assertFalse(sc.compatible("set_volume", opts["e:light.salon_pie"]))

    def test_canonical(self):
        a = sc.canonical("cover_position", ["cover.x"], 0)
        self.assertEqual(a, sc.canonical("close_cover", ["cover.x"], None))
        self.assertEqual(sc.canonical("turn_on", ["cover.x"], None), sc.canonical("open_cover", ["cover.x"], None))
        self.assertNotEqual(sc.canonical("turn_on", ["light.x"], None), sc.canonical("turn_off", ["light.x"], None))
        self.assertEqual(sc.canonical("light_set", ["light.x"], 40.0).value, 40)
        self.assertEqual(sc.canonical("climate_set", ["climate.x"], 21.5).value, 21.5)
        self.assertEqual(sc.canonical("volume_up", ["m.x"], None).value, "up")

    def test_ha_call(self):
        opts = {o.key: o for o in sc.target_options(HOME)}
        self.assertEqual(sc.ha_call("light_set", opts["a:salon:light"], 40, HOME),
                         ("HassLightSet", {"area": "Salón", "domain": ["light"], "brightness": 40}))
        self.assertEqual(sc.ha_call("turn_off", opts["e:light.cocina_techo"], None, HOME),
                         ("HassTurnOff", {"name": "Luz del techo", "area": "Cocina"}))
        self.assertEqual(sc.ha_call("start_timer", None, 5400, HOME),
                         ("HassStartTimer", {"hours": 1, "minutes": 30}))


class GateTest(unittest.TestCase):
    def setUp(self):
        self.opts = sc.target_options(HOME)
        self.kind = {"command": 0.97, "question": 0.02, "other": 0.01}
        self.gate = sc.Gate(0.9, 0.9, 0.9)

    def decide(self, kind, intent, target, text="Pon la luz del salón al 40"):
        return sc.decide(kind, intent, target, self.opts, ne.parse_value, text, self.gate)

    def test_execute(self):
        d = self.decide(self.kind, {"light_set": 0.95, "turn_on": 0.05}, {"a:salon:light": 0.96, "h:light": 0.04})
        self.assertTrue(d.act)
        self.assertEqual(d.action, sc.canonical("light_set", ["light.salon_techo", "light.salon_pie"], 40))

    def test_not_a_command(self):
        d = self.decide({"command": 0.4, "question": 0.6, "other": 0.0}, {"turn_on": 1.0}, {"a:salon:light": 1.0})
        self.assertFalse(d.act)

    def test_low_intent(self):
        self.assertFalse(self.decide(self.kind, {"turn_on": 0.6, "turn_off": 0.4}, {"a:salon:light": 1.0}).act)

    def test_missing_value(self):
        d = self.decide(self.kind, {"light_set": 0.99}, {"a:salon:light": 0.99}, text="Pon la luz del salón")
        self.assertFalse(d.act)
        self.assertIn("value", d.reason)

    def test_incompatible_target_mass(self):
        # The model prefers a light although the intent is a cover action: hand off.
        d = self.decide(self.kind, {"open_cover": 0.99}, {"e:light.salon_pie": 0.8, "e:cover.salon_persiana": 0.2})
        self.assertFalse(d.act)

    def test_timer_needs_no_target(self):
        d = self.decide(self.kind, {"start_timer": 0.99}, {}, text="Pon un temporizador de diez minutos")
        self.assertTrue(d.act)
        self.assertEqual(d.action.value, 600)


class GeneratorTest(unittest.TestCase):
    def test_render(self):
        import gen_data as gd
        slots = {"g": "f", "np": "la lámpara de pie", "area_np": "el salón", "value": 40}
        self.assertEqual(gd.render("Pon {t} al {pct} por ciento", slots, "digits"),
                         "Pon la lámpara de pie al 40 por ciento")
        self.assertEqual(gd.render("Pon {t} al {pct} por ciento", slots, "words"),
                         "Pon la lámpara de pie al cuarenta por ciento")
        self.assertEqual(gd.render("{t}, enciénde{lo}", slots, "digits"), "La lámpara de pie, enciéndela")
        self.assertEqual(gd.render("Quiero {t} {adj:encendid}", dict(slots, g="mp", np="los focos"), "digits"),
                         "Quiero los focos encendidos")
        self.assertEqual(gd.render("Apágalo todo {en_area}", slots, "digits"), "Apágalo todo en el salón")
        self.assertEqual(gd.render("Sube el volumen {de_t}", dict(slots, np=""), "digits"), "Sube el volumen")
        self.assertEqual(gd.de("el salón"), "del salón")
        self.assertEqual(gd.a_("el altavoz"), "al altavoz")


class CategoryTest(unittest.TestCase):
    def test_category_rules(self):
        try:
            import headroom as hr
        except ImportError as exc:  # needs the agent_eval / small_models siblings
            self.skipTest(str(exc))
        self.assertEqual(hr.category(True, "lost"), "a")
        self.assertEqual(hr.category(False, "lost-verb"), "a-verb")
        self.assertEqual(hr.category(False, "exact"), "b1")
        self.assertEqual(hr.category(False, "fuzzy"), "b1")
        self.assertEqual(hr.category(True, "fuzzy"), "b2")
        self.assertEqual(hr.category(True, "exact"), "c")

    def test_keyword_status(self):
        try:
            import headroom as hr
            import rapidfuzz  # noqa: F401
            hr.norm_words("x")
        except Exception as exc:  # noqa: BLE001  (optional dependencies)
            self.skipTest(str(exc))
        self.assertEqual(hr.keyword_status("Baja la persiana del salón", "Baja la persiana del salón.")[0], "exact")
        self.assertEqual(hr.keyword_status("Baja la persiana del salón", "Baja la pesiana del salón.")[0], "fuzzy")
        self.assertEqual(hr.keyword_status("Quita el silencio de la tele", "Mirá.")[0], "lost")
        self.assertEqual(hr.keyword_status("Pon un temporizador de diez minutos",
                                           "Con un temporizador de 10 minutos.")[0], "lost-verb")
        self.assertEqual(hr.keyword_status("Pon la luz al 40", "Pon la luz al 14")[0], "lost")


class ParaphraseValidationTest(unittest.TestCase):
    def setUp(self):
        try:
            import paraphrase  # noqa: F401
            import rapidfuzz  # noqa: F401
        except ImportError as exc:
            self.skipTest(str(exc))
        self.item = {"text": "Pon la lámpara de pie del salón al 40 por ciento",
                     "actions": [{"intent": "light_set", "target": "e:light.salon_pie", "value": 40}]}

    def test_validate(self):
        import paraphrase as pp
        self.assertEqual(pp.validate(self.item, "¿Me pones la lámpara de pie del salón al cuarenta por ciento?", HOME), "")
        self.assertEqual(pp.validate(self.item, "Pon la lámpara de pie del salón al 50 %", HOME), "value 50 != 40")
        self.assertEqual(pp.validate(self.item, "Pon la luz al 40 por ciento", HOME), "target name lost")
        self.assertEqual(pp.validate(self.item, "Pon la lámpara de pie al 40 por ciento", HOME), "area lost")
        self.assertEqual(pp.validate(self.item, self.item["text"], HOME), "identical")
        tele = {"text": "Enciende la lámpara de pie", "actions": [{"intent": "turn_on", "target": "e:light.salon_pie",
                                                                    "value": None}]}
        self.assertEqual(pp.validate(tele, "Enciende la lámpara de pie de la cocina", HOME), "other area added")
        off = {"text": "Enciende la lámpara de pie", "actions": [{"intent": "turn_on", "target": "e:light.salon_pie",
                                                                   "value": None}]}
        self.assertEqual(pp.validate(off, "Apaga la lámpara de pie", HOME), "intent cue lost")
        self.assertEqual(pp.validate(off, "Enciende la lámpara de pie y apaga la tele", HOME), "two requests")


if __name__ == "__main__":
    unittest.main()
