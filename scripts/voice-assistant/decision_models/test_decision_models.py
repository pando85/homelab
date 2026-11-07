"""Self-tests of scripts/voice-assistant/decision_models (pure logic; no model, no network).

Run: python3 -m unittest discover -s scripts/voice-assistant/decision_models -p 'test_decision_models.py'
Needs pyyaml and rapidfuzz; the oracle test also needs agent_eval (pyyaml only).
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "agent_eval"))
import decide as D  # noqa: E402


def fake_home():
    import yaml
    with open(os.path.join(HERE, "..", "agent_eval", "inventory_es.yaml"), encoding="utf-8") as fh:
        return D.Home(yaml.safe_load(fh))


# Hand-labelled correct decisions for the reference texts of the control phrases (fake home).
ORACLE = {
    "c01": ("orden", "encender", "a:salon", "luz", "todos"),
    "c04": ("orden", "encender", "aqui", "luz", "e:light.salon_lampara"),
    "c05": ("orden", "fijar", "a:salon", "luz", "todos"),
    "c06": ("orden", "fijar", "aqui", "luz", "e:light.salon_lampara"),
    "c07": ("orden", "fijar", "aqui", "luz", "e:light.cocina_encimera"),
    "c09": ("orden", "apagar", "casa", "luz", None),
    "c10": ("orden", "apagar", "a:salon", "persiana", None),
    "c13": ("orden", "fijar", "a:salon", "persiana", None),
    "c14": ("orden", "pausar", "aqui", "toldo", None),
    "c15": ("orden", "fijar", "a:salon", "clima", None),
    "c16": ("orden", "fijar", "a:dormitorio", "clima", None),
    "c18": ("orden", "fijar", "aqui", "tele", None),
    "c19": ("orden", "subir_volumen", "aqui", "tele", None),
    "c24": ("orden", "reanudar", "aqui", "tele", None),
    "c26": ("orden", "siguiente", "a:cocina", "altavoz", None),
    "c27": ("temporizador", "iniciar"),
    "c28": ("temporizador", "iniciar"),
    "c33": ("orden", "apagar", "a:dormitorio", "ventilador", None),
    "c34": ("orden", "fijar", "aqui", "ventilador", None),
    "c38": ("orden", "encender", "aqui", "luz", "todos"),
    "c39": ("orden", "apagar", "aqui", "tele", None),
    "c40": ("orden", "bajar_volumen", "aqui", "tele", None),
    "c41": ("orden", "apagar", "a:cocina", "todo", None),
}


def oracle_ask(dec):
    """A backend that answers every question with the oracle's option at probability 0.9."""
    if dec[0] == "temporizador":
        want = {"peticion": "temporizador", "temporizador": dec[1]}
    else:
        want = dict(zip(("peticion", "accion", "zona", "aparato", "cual"), dec))

    def ask(text, questions):
        out = {}
        for qid, q in questions.items():
            opts = list(q["criteria"])
            w = want.get(qid.split("#")[0])
            pick = w if w in opts else opts[0]
            out[qid] = {k: (0.9 if k == pick else 0.1 / (len(opts) - 1)) for k in opts}
        return out
    return ask


class NumbersTest(unittest.TestCase):
    def test_values(self):
        self.assertEqual(D.value_of("Pon la luz del salón al cuarenta por ciento"), 40)
        self.assertEqual(D.value_of("Pon la luz del salón al 40%"), 40)
        self.assertEqual(D.value_of("Pon la temperatura del dormitorio a veintiuno y medio"), 21.5)
        self.assertEqual(D.value_of("pon el aire a 22,5 grados"), 22.5)
        self.assertEqual(D.value_of("Pon la persiana del salón a la mitad"), 50)
        self.assertEqual(D.value_of("Pon la luz de la encimera al máximo"), 100)
        self.assertEqual(D.value_of("sube el brillo al treinta y cinco"), 35)
        self.assertIsNone(D.value_of("Enciende la luz"))
        self.assertIsNone(D.value_of("Enciende una luz"))

    def test_durations(self):
        self.assertEqual(D.duration_seconds("Pon un temporizador de diez minutos"), 600)
        self.assertEqual(D.duration_seconds("Pon un temporizador de una hora y media"), 5400)
        self.assertEqual(D.duration_seconds("temporizador de media hora"), 1800)
        self.assertEqual(D.duration_seconds("Pon un temporizador de 8 minutos"), 480)
        self.assertEqual(D.duration_seconds("un minuto y medio"), 90)
        self.assertIsNone(D.duration_seconds("pon un temporizador"))

    def test_fold(self):
        self.assertEqual(D.fold("¿Qué tal, Salón?"), "que tal salon")


class OptionsTest(unittest.TestCase):
    def test_split(self):
        crit = {str(i): "" for i in range(23)}
        parts = D.split_options(crit, 10)
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(len(p) <= 10 for p in parts))
        self.assertEqual(sum(len(p) for p in parts), 23)
        self.assertEqual(D.split_options({"a": 1}, 10), [{"a": 1}])

    def test_first_pass_fits_ten(self):
        qs = D.first_pass(fake_home(), "es")
        self.assertTrue(all(len(q["criteria"]) <= 10 for q in qs.values()))
        self.assertIn("a:salon", qs["zona"]["criteria"])
        self.assertIn("todo", qs["aparato"]["criteria"])

    def test_knockout_uses_two_calls(self):
        home = fake_home()
        calls = []

        def ask(text, questions):
            calls.append(set(questions))
            return {q: {k: 1 / len(v["criteria"]) for k, v in [(q, v)] for k in v["criteria"]}
                    for q, v in questions.items()}
        D.decide(ask, home, "hola", "es", max_options=3)
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(any(q.startswith("zona#") for q in calls[0]))

    def test_gate_is_minimum(self):
        d = {"peticion": "orden", "accion": "encender", "zona": "aqui", "aparato": "luz", "cual": None}
        conf = {"peticion": 0.99, "accion": 0.8, "zona": 0.95, "aparato": 0.9, "temporizador": 0.1}
        self.assertAlmostEqual(D.gate_confidence(d, conf), 0.8)
        self.assertAlmostEqual(D.gate_confidence({"peticion": "otra"}, {"peticion": 0.7}), 0.7)


class MappingTest(unittest.TestCase):
    def test_incompatible_hands_off(self):
        home = fake_home()
        d = {"peticion": "orden", "accion": "subir_volumen", "zona": "aqui", "aparato": "luz", "cual": None}
        self.assertIsNone(D.to_call(home, d, "sube la luz")[0])
        d = {"peticion": "orden", "accion": "fijar", "zona": "aqui", "aparato": "luz", "cual": "todos"}
        self.assertIsNone(D.to_call(home, d, "pon la luz")[0])        # no number
        d = {"peticion": "otra"}
        self.assertIsNone(D.to_call(home, d, "quién pintó las meninas")[0])

    def test_here_prefers_satellite_area(self):
        home = fake_home()
        self.assertEqual(sorted(D.resolve_targets(home, "aqui", "luz", "todos")),
                         ["light.salon_lampara", "light.salon_techo"])
        self.assertEqual(D.resolve_targets(home, "aqui", "ventilador"), ["fan.dormitorio_ventilador"])
        self.assertIsNone(D.resolve_targets(home, "a:bano", "tele"))

    def test_outcome(self):
        self.assertEqual(D.outcome(None, "act", None), "handoff")
        self.assertEqual(D.outcome({"name": "x"}, "noact", None), "wrong")
        self.assertEqual(D.outcome({"name": "x"}, "act", lambda c: True), "rescued")

    def test_mcnemar(self):
        self.assertEqual(D.mcnemar_p(0, 0), 1.0)
        self.assertAlmostEqual(D.mcnemar_p(12, 3), 0.0352, places=4)


class AdaptersTest(unittest.TestCase):
    def test_fake_v1_expect(self):
        import decision_eval as E
        it = {"kind": "command", "actions": [{"intent": "light_set", "target": "e:light.x", "value": 40}],
              "entities": [["light.x"]]}
        gold, exp = E.fake_v1_expect(it)
        self.assertEqual(gold, "act")
        self.assertEqual(exp, {"tool": "light__HassLightSet", "targets": ["light.x"], "args": {"brightness": 40}})
        call = {"name": "light__HassLightSet", "args": {"brightness": 40}, "ok": True, "targets": ["light.x"],
                "meta_args": {"brightness": 40}}
        self.assertTrue(E.make_grader(exp)(call))
        gold, exp = E.fake_v1_expect({"kind": "command", "actions": [
            {"intent": "close_cover", "target": "e:cover.y", "value": None}], "entities": [["cover.y"]]})
        self.assertEqual(len(exp["any_of"]), 2)
        self.assertEqual(E.fake_v1_expect({"kind": "multi", "actions": [{}, {}], "entities": []})[0], "noact")
        self.assertEqual(E.fake_v1_expect({"kind": "question", "actions": [], "entities": []})[0], "noact")

    def test_neutral_keys_round_trip(self):
        import backends
        seen = {}

        def ask(text, questions):
            seen.update(questions)
            return {q: {k: (0.7 if k == "B" else 0.3 / (len(v["criteria"]) - 1)) for k in v["criteria"]}
                    for q, v in questions.items()}
        out = backends.neutral_keys(ask)("x", {"q": D.choice("?", {"encender": "on", "apagar": None, "otra": "x"})})
        self.assertEqual(list(seen["q"]["criteria"]), ["A", "B", "C"])
        self.assertEqual(seen["q"]["criteria"]["B"], "apagar")   # a description-less option keeps its key as text
        self.assertEqual(max(out["q"], key=out["q"].get), "apagar")

    def test_area_wording(self):
        import real_phrases as R
        self.assertEqual(R.de("Cocina"), "de la cocina")
        self.assertEqual(R.de("Salón"), "del salón")
        self.assertEqual(R.en("Salón"), "en el salón")
        self.assertEqual(R.en("Cocina"), "en la cocina")


class OracleTest(unittest.TestCase):
    """With correct answers to every question, the mapping must produce a passing call: checks the
    option keys, target resolution, number parsing and the grading glue together."""

    def test_oracle_rescues_reference_texts(self):
        import decision_eval as E
        home = fake_home()
        ph = E.load_phrases()
        for pid, dec in ORACLE.items():
            r = D.decide(oracle_ask(dec), home, ph[pid]["text"], "es")
            self.assertIsNotNone(r["call"], f"{pid}: {r['reason']} {r['decision']}")
            self.assertTrue(E.make_grader(ph[pid]["expect"])(r["call"]), f"{pid}: {r['call']}")


if __name__ == "__main__":
    unittest.main()
