"""Tests of the pure parts of kivi_fair_fa.py (window matching, event merging, cells, paired counts). Synthetic only.

    cd scripts/voice-assistant/wakeword && $KIVI_HOME/.venv/bin/python -m pytest test_kivi_fair_fa.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kivi_fair_fa as F  # noqa: E402

T = 1_794_340_800.0           # 2026-11-10T20:00Z = 21:00 Madrid on the 10th (evening, info day)


def test_window_class_attempt_wins_and_unlabelled_is_conservative():
    w = [{"device": "c3-a", "t": T, "label": "attempt"}, {"device": "c3-a", "t": T + 1, "label": "other-speech"},
         {"device": "c3-b", "t": T + 50, "label": "noise"}, {"device": "c3-b", "t": T + 100, "label": "unclear"},
         {"device": "c3-b", "t": T + 200, "label": ""}]
    assert F.window_class(w, "c3-a", T + 2.5) == ("attempt", 2)
    assert F.window_class(w, "c3-b", T + 52) == ("false", 1)
    assert F.window_class(w, "c3-b", T + 100) == ("unclear", 1)
    assert F.window_class(w, "c3-b", T + 200) == ("unlabelled", 0)          # an unlabelled window counts as a false wake
    assert F.window_class(w, "c3-a", T + 50) == ("unlabelled", 0)           # other TV: no match


def test_room_events_merge_models_and_cells_count_a_and_b():
    room = [{"pieces_detail": [{"device": "c3-a", "start": T - 100, "hours_by_date": {"2026-11-10": 2.0},
                                "evening_h_by_date": {"2026-11-10": 1.5},
                                "times": {"v5": [100.0], "v6.2": [101.0, 400.0], "x": [5.0]}}]}]
    ev = F.room_events(room, ["v5", "v6", "v6.2"])
    assert [(e["t"] - T, sorted(e["models"])) for e in ev] == [(0.0, ["v5", "v6.2"]), (300.0, ["v6.2"])]
    for e in ev:
        e["cls"], e["n_labels"] = F.window_class([{"device": "c3-a", "t": T, "label": "attempt"}], e["device"], e["t"])
    clips = [{"device": "c3-a", "day": "2026-11-10", "evening": True, "cls": "false", "fires": {"v5"}, "recall": set(),
              "scored": {"v5", "v6.2"}, "source": "owner", "trigger": "wake"},
             {"device": "c3-a", "day": "2026-11-10", "evening": False, "cls": "not-a-wake", "fires": {"v6.2"}, "recall": set(),
              "scored": {"v5", "v6.2"}, "source": "owner", "trigger": "button"}]
    h = F.room_hours(room)
    c5 = F.cell("v5", "c3-a", "2026-11-10", ev, clips, h)
    c62 = F.cell("v6.2", "c3-a", "2026-11-10", ev, clips, h)
    assert (c5["a"], c5["b"], c5["total_evening"], c5["attempts_excluded"]) == (0, 1, 1, 1)
    assert (c62["a"], c62["b"], c62["total_evening"], c62["notawake_fired"]) == (1, 0, 1, 1)
    assert c62["evenings"] == 0.25 and c62["per_evening"] == 4.0
    p = F.paired("v6.2", "v5", ["2026-11-10"], ev, clips, False)
    assert (p["both"], p["only_cand"], p["only_ref"]) == (0, 1, 1)           # the attempt is no unit; the live clip is v5 only


def test_paired_per_device_one_sided_and_gate_block():
    ev = [{"device": "c3-b", "day": "2026-11-13", "evening": True, "cls": "false", "models": {"v6"}},
          {"device": "c3-b", "day": "2026-11-13", "evening": True, "cls": "unlabelled", "models": {"v6"}},
          {"device": "c3-b", "day": "2026-11-13", "evening": True, "cls": "attempt", "models": {"v6.3"}},
          {"device": "c3-a", "day": "2026-11-13", "evening": True, "cls": "false", "models": {"v6.3"}}]
    clips = [{"device": "c3-b", "day": "2026-11-13", "evening": True, "cls": "false", "fires": {"v6"}, "scored": {"v6", "v6.3"}}
             for _ in range(4)]
    pb = F.paired("v6.3", "v6", ["2026-11-13"], ev, clips, True, device="c3-b")
    assert (pb["both"], pb["only_cand"], pb["only_ref"]) == (0, 0, 6)       # the attempt never counts
    assert pb["p_one_sided_fewer"] == round(0.5 ** 6, 4)
    pa = F.paired("v6.3", "v6", ["2026-11-13"], ev, clips, True, device="c3-a")
    assert (pa["only_cand"], pa["only_ref"]) == (1, 0)
    res = {"paired": {"v6.3-vs-v6|test|evening|c3-b": pb},
           "pooled": {"v6.3|test|c3-b": {"evenings": 1.0, "total_evening": 0, "a_unlabelled": 0},
                      "v6|test|c3-b": {"evenings": 1.0, "total_evening": 6},
                      "v6.3|test|both": {"total": 1}, "v6|test|both": {"total": 6}},
           "recall": {"test": {"v6.3": {"all_calls": [5, 6]}, "v6": {"all_calls": [5, 6]}}}}
    g = F.gate_block(res, {"cand": "v6.3", "ref": "v6", "device": "c3-b"})
    assert g["primary_shown"] and g["not_worse"] and g["recall_ok"] and g["total_ok"]


def test_apply_config_puts_the_reference_first(tmp_path):
    import json
    keep = (F.MODELS, F.DAYS, F.RESERVED, F.SINCE, F.ROOM_RUNS, F.OUT, F.REF, F.GATE)
    cfg = {"models": {"v6.3": ["a.tflite", 0.99, "x"], "v6": ["b.tflite", 0.9595, "oye_kivi_ctx"]}, "days": ["2026-11-13"],
           "room_runs": [["d1113", "negatives-daily:d-20261113", "2026-11-13"]], "out": "v6p3/gate/fair", "ref": "v6",
           "gate": {"cand": "v6.3", "ref": "v6", "device": "c3-b"}}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(cfg))
    try:
        F.apply_config(p)
        assert list(F.MODELS) == ["v6", "v6.3"] and F.DAYS == ("2026-11-13",) and F.RESERVED == () and F.REF == "v6"
        assert F.OUT == F.KH / "v6p3/gate/fair" and F.GATE["device"] == "c3-b"
    finally:
        F.MODELS, F.DAYS, F.RESERVED, F.SINCE, F.ROOM_RUNS, F.OUT, F.REF, F.GATE = keep
