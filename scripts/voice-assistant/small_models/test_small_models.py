"""Self-tests of scripts/voice-assistant/small_models (pure functions; no model, no server, no audio).

Run: python3 -m unittest discover -s scripts/voice-assistant/small_models -p 'test_small_models.py'
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import corrector_eval  # noqa: E402
import router_eval  # noqa: E402
import stt_eval  # noqa: E402


class StatsTest(unittest.TestCase):
    def test_mcnemar_exact(self):
        self.assertEqual(stt_eval.mcnemar_p(0, 0), 1.0)
        self.assertAlmostEqual(stt_eval.mcnemar_p(12, 3), 0.0352, places=4)
        self.assertAlmostEqual(stt_eval.mcnemar_p(3, 12), stt_eval.mcnemar_p(12, 3))
        self.assertEqual(stt_eval.mcnemar_p(5, 5), 1.0)

    def test_wilson(self):
        lo, hi = stt_eval.wilson(119, 145)
        self.assertTrue(0.74 < lo < 0.76 and 0.87 < hi < 0.88)


class RouterTest(unittest.TestCase):
    def test_score_counts(self):
        items = [{"id": "c1", "label": "casa"}, {"id": "c2", "label": "casa"},
                 {"id": "g1", "label": "kiais"}, {"id": "g2", "label": "kiais"}]
        s = router_eval.score(["casa", "kiais", "kiais", "casa"], items)
        self.assertEqual(s["acc"], "2/4 (50%)")
        self.assertEqual(s["casa_kept"], "1/2")
        self.assertEqual(s["kiais_sent"], "1/2")
        self.assertEqual(s["wrong_ids"], "c2 g2")

    def test_training_set_disjoint_from_names(self):
        train = [t for ts in router_eval.ROUTER_TRAIN.values() for t in ts]
        self.assertEqual(len(train), len(set(train)))
        self.assertEqual(len(router_eval.ROUTER_TRAIN["casa"]), len(router_eval.ROUTER_TRAIN["kiais"]))

    def test_cosine_helpers(self):
        v = router_eval.norm([3.0, 4.0])
        self.assertAlmostEqual(router_eval.dot(v, v), 1.0)


class CorrectorTest(unittest.TestCase):
    def test_simple_normalization(self):
        self.assertEqual(corrector_eval.simple("¿Qué tal, Salón?"), "que tal salon")
        self.assertEqual(corrector_eval.simple("Apaga  la luz."), corrector_eval.simple("apaga la luz"))


if __name__ == "__main__":
    unittest.main()
