#!/usr/bin/env python3
"""Offline test of apps/home-assistant/config/custom_sentences/es/tv_open_app.yaml.

Mimics what Home Assistant 2026.11.0's default agent does (components/conversation/default_agent.py):
the built-in Spanish intents of home-assistant-intents plus the custom file, whose sentences are
flagged with the metadata key hass_custom_sentence, matched with hassil's recognize_best
(best_metadata_key=hass_custom_sentence, best_slot_name="name") and slot lists for "area", "name"
and "floor". Nothing talks to Home Assistant, and the result is an inference from hassil alone
(the intent handlers, the script and the area registry are not exercised).

Needs the versions HA 2026.11.0 pins (package_constraints.txt, conversation/manifest.json):
  python3 -m venv /tmp/v && /tmp/v/bin/pip install hassil==3.12.1 home-assistant-intents==2026.10.27 pyyaml
  /tmp/v/bin/python scripts/voice-assistant/tv_open_app/test_tv_open_app_sentences.py        # table
  /tmp/v/bin/python -m unittest discover -s scripts/voice-assistant/tv_open_app -p 'test_tv_open_app_sentences.py'
"""
import os
import re
import sys
import unittest

import yaml
from hassil import Intents, recognize_best
from hassil.intents import TextSlotList
import home_assistant_intents as hai

HERE = os.path.dirname(os.path.abspath(__file__))
SENTENCES = os.path.join(HERE, "..", "..", "..", "apps", "home-assistant", "config", "custom_sentences",
                         "es", "tv_open_app.yaml")
METADATA_CUSTOM_SENTENCE = "hass_custom_sentence"  # homeassistant/components/conversation/const.py

# Fake household: the two TV areas (name, then the aliases the owner set), a kitchen, and exposed names
# for the collision tests. The real area names are not known here; the area slot value is the spoken
# name or alias, exactly as Home Assistant builds the list (default_agent._make_slot_lists).
AREAS = ["Living room", "salón", "Office A", "oficina grande", "oficina A", "Casa Norte", "Cocina"]
NAMES = [("persiana", "persiana", {"domain": "cover"}),
         ("luz del salón", "luz del salón", {"domain": "light"}),
         ("televisión", "televisión", {"domain": "media_player"})]


def remove_punctuation(text):
    return re.sub(r"[¿?¡!.,;:]", " ", text).strip()


def build_matcher(path=SENTENCES):
    """path=None builds the baseline: built-in intents only (what runs without the custom file)."""
    data = hai.get_intents("es")
    if path is None:
        custom = {"intents": {}, "lists": {}, "expansion_rules": {}}
    else:
        with open(path, encoding="utf-8") as f:
            custom = yaml.safe_load(f)
    for intent_dict in custom.get("intents", {}).values():  # same flagging as _load_intents
        for item in intent_dict.get("data", []):
            md = item.get("metadata", {})
            md[METADATA_CUSTOM_SENTENCE] = True
            item["metadata"] = md
    merged = dict(data)
    merged["intents"] = dict(data["intents"])
    merged["intents"].update(custom["intents"])
    merged["lists"] = dict(data.get("lists", {}))
    merged["lists"].update(custom.get("lists", {}))
    merged["expansion_rules"] = dict(data.get("expansion_rules", {}))
    merged["expansion_rules"].update(custom.get("expansion_rules", {}))
    intents = Intents.from_dict(merged)
    slot_lists = {
        "area": TextSlotList.from_tuples([(remove_punctuation(a), a) for a in AREAS], allow_template=False),
        "name": TextSlotList.from_tuples([(remove_punctuation(n), o, c) for n, o, c in NAMES], allow_template=False),
        "floor": TextSlotList.from_tuples([], allow_template=False),
    }

    def match(text):
        return recognize_best(remove_punctuation(text), intents, slot_lists=slot_lists, language="es",
                              best_metadata_key=METADATA_CUSTOM_SENTENCE, best_slot_name="name")
    return match


def describe(result):
    if result is None:
        return None, {}
    return result.intent.name, {k: str(v.value) for k, v in result.entities.items()}


# (phrase, expect TvOpenApp?, expected slots subset, strict: counts for pass/fail)
CASES = [
    ("abre YouTube", True, {"app": "YouTube"}, True),
    ("Abre YouTube en la tele del salón", True, {"app": "YouTube", "area": "salón"}, True),
    ("abre youtube en el salón", True, {"app": "YouTube", "area": "salón"}, True),
    ("quiero ver Netflix", True, {"app": "Netflix"}, True),
    ("abre YouTube en la tele de la oficina grande", True, {"app": "YouTube", "area": "oficina grande"}, True),
    ("abre la persiana", False, {}, True),
    ("pon música de Queen", False, {}, True),
    ("pon el volumen al 20", False, {}, True),
    ("quiero ver el partido", False, {}, True),
    # Extra phrases.
    ("abre YouTube en la tele", True, {"app": "YouTube"}, True),
    ("pon Netflix en la tele del salón", True, {"app": "Netflix", "area": "salón"}, True),
    ("ponme Prime Video en la cocina", True, {"app": "Prime Video", "area": "Cocina"}, True),
    ("abre Spotify por favor", True, {"app": "Spotify – Música y podcasts"}, True),
    ("quiero ver Disney+ en la tele de la oficina A", True, {"app": "Disney+", "area": "oficina A"}, True),
    ("abre el navegador", True, {"app": "Navegador web"}, True),
    ("abre youtube sin anuncios en el salón", True, {"app": "YouTube AdFree", "area": "salón"}, True),
    ("abre la ventana", False, {}, True),
    ("pon la luz del salón", False, {}, True),
    ("pon música", False, {}, True),
    ("abre Netflix y apaga la luz", False, {}, True),
    # Whisper-ish spellings, reported but not enforced (hassil is accent sensitive).
    ("abre Youtube", True, {"app": "YouTube"}, False),
    ("Abre YouTube en la tele del salon", True, {"app": "YouTube", "area": "salón"}, False),
    ("abre you tube", True, {"app": "YouTube"}, False),
    ("abre Disney plus", True, {"app": "Disney+"}, False),
    ("abre YouTube en la oficina", True, {"app": "YouTube"}, False),
]


def evaluate(match, phrase, want, slots):
    intent, got = describe(match(phrase))
    is_tv = intent == "TvOpenApp"
    ok = is_tv == want and (not want or all(got.get(k) == v for k, v in slots.items()))
    return ok, intent, got


class TvOpenAppSentences(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.match = staticmethod(build_matcher())

    def test_enforced_cases(self):
        for phrase, want, slots, strict in CASES:
            if not strict:
                continue
            with self.subTest(phrase=phrase):
                ok, intent, got = evaluate(self.match, phrase, want, slots)
                self.assertTrue(ok, f"{phrase!r}: intent={intent} slots={got}, expected TvOpenApp={want} {slots}")


    def test_negatives_unchanged_by_the_custom_file(self):
        """A phrase that is not for TvOpenApp must give the same intent and slots with and without it."""
        base = build_matcher(None)
        for phrase, want, _slots, _strict in CASES:
            if want:
                continue
            with self.subTest(phrase=phrase):
                self.assertEqual(describe(base(phrase)), describe(self.match(phrase)))


def main():
    match = build_matcher()
    print(f"{'phrase':58} {'matched intent':22} {'slots':50} result")
    bad = 0
    for phrase, want, slots, strict in CASES:
        ok, intent, got = evaluate(match, phrase, want, slots)
        tag = "pass" if ok else ("FAIL" if strict else "fail (not enforced)")
        bad += (not ok) and strict
        print(f"{phrase:58} {str(intent):22} {str(got):50} {tag}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
