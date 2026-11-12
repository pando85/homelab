"""Tests of kivi_clean.py (pure parts; no audio, no models)."""
from datetime import datetime, timezone

import kivi_clean as K
import kivi_label_queue as Q


def ep(iso):
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()


def test_day_rule_and_groups():
    assert K.is_train_day("2026-11-05") and K.is_train_day("2026-11-07")          # grandfathered
    assert K.is_train_day("2026-11-12") and not K.is_train_day("2026-11-13")       # 11-12 released, 11-13 test
    assert K.room_group("c3-b", ep("2026-11-12T18:30:00")) == "room:c3-b:2026-11-12T19"


def test_folds_stable_and_positive_groups_spread():
    groups = {f"g{i}" for i in range(50)}
    pos = {f"g{i}" for i in range(10)}
    f1, f2 = K.assign_folds(groups, pos), K.assign_folds(groups, pos)
    assert f1 == f2
    assert sorted(f1[g] for g in pos) == [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]


def test_merge_events():
    ev = K.merge_events([("c3-b", 10.0, 0.8), ("c3-b", 11.5, 0.95), ("c3-b", 20.0, 0.9), ("c3-a", 10.5, 0.9)])
    assert len(ev) == 3
    b = [e for e in ev if e["device"] == "c3-b"]
    assert b[0]["t"] == 11.5 and b[0]["n"] == 2 and b[0]["score"] == 0.95


def test_evidence_and_asr_flags():
    assert K.evidence_from_autolabel("ph=1;weak=0;kivi=0;oye=1") == "phrase"
    assert K.evidence_from_autolabel("ph=0;weak=0;kivi=1;oye=0") == "kivi_word"
    assert K.evidence_from_autolabel("ph=0;weak=0;kivi=0;oye=1") == "weak"
    assert K.evidence_from_autolabel("ph=0;weak=0;kivi=0;oye=0") == "none"
    assert K.evidence_from_autolabel("trig=button") is None
    none = {"kind": "none", "end_off_s": None, "word_prob": None}
    assert K.asr_flag({"kind": "strict", "end_off_s": 0.3, "word_prob": 0.9}, none) == ("phrase", 0.9)
    assert K.asr_flag({"kind": "strict", "end_off_s": 2.5, "word_prob": 0.9}, none) == ("none", None)   # too far
    assert K.asr_flag({"kind": "kivi", "end_off_s": None, "word_prob": 0.7}, none) == ("kivi_word", 0.7)  # anywhere
    assert K.asr_flag(none, {"kind": "strict", "end_off_s": 0.1, "word_prob": 0.4}) == ("hotword_only", 0.4)


def test_analyse_words():
    w = [("oye", 0.0, 0.3, 0.9), ("kivi", 0.35, 0.8, 0.8)]
    assert K.analyse_words(w, 1.0) == {"kind": "strict", "end_off_s": -0.2, "word_prob": 0.8}
    assert K.analyse_words([("kiwi", 0.0, 0.5, 0.6)], None)["kind"] == "kivi"
    assert K.analyse_words([("mira", 0.0, 0.5, 0.6)], 1.0)["kind"] == "none"


def test_drop_rule_section_9():
    assert K.drop_by_asr("phrase", None, "mined") and K.drop_by_asr("kivi_word", 0.2, "win_neg")
    assert K.drop_by_asr("hotword_only", 0.1, "room_w8") and not K.drop_by_asr("hotword_only", 0.4, "mined")
    assert K.drop_by_asr("hotword_only", 0.6, "mined")
    assert not K.drop_by_asr("weak", 1.0, "mined") and not K.drop_by_asr("none", None, "mined")


def test_decide():
    base = {"cls": 0, "set": "mined", "oof": 0.1, "asr": "none", "source": "", "pending": False}
    assert K.decide(base, 0.7, 0.05) == ("ok", "")
    assert K.decide(dict(base, asr="phrase"), 0.7, 0.05)[0] == "auto-drop"
    assert K.decide(dict(base, oof=0.9), 0.7, 0.05) == ("owner", "oof high")
    assert K.decide(dict(base, oof=0.9, source="owner"), 0.7, 0.05)[0] == "owner-label-stands"
    assert K.decide(dict(base, oof=0.9, pending=True), 0.7, 0.05)[0] == "already-queued"
    assert K.decide(dict(base, oof=0.6, asr="weak"), 0.7, 0.05) == ("owner", "asr weak")
    pos = {"cls": 1, "set": "live_pos", "oof": 0.01, "asr": "phrase", "source": "auto-v1", "pending": False}
    assert K.decide(pos, 0.7, 0.05) == ("owner", "oof low")
    assert K.decide(dict(pos, asr="phrase", oof=0.9), 0.7, 0.05)[0] == "ok"
    assert K.decide(dict(pos, asr="phrase"), 0.7, 0.05)[0] != "auto-drop"          # positives are never auto-dropped


def test_needs_asr():
    assert K.needs_asr({"set": "mined", "id": "a", "prior": None}, set())
    assert not K.needs_asr({"set": "mined", "id": "a", "prior": ["none", None]}, set())
    assert K.needs_asr({"set": "mined", "id": "a", "prior": ["none", None]}, {"a"})
    assert not K.needs_asr({"set": "win_neg", "id": "b", "prior": ["owner:noise", None]}, {"b"})
    assert not K.needs_asr({"set": "room_new", "id": "c", "prior": None}, {"c"})


def test_p0_tier_sorts_first():
    it0 = Q._plan_item("window", "cl-00001", "P0", "c3-a", "20261112T100000.000Z", "train", "0")
    it1 = Q._plan_item("window", "lq-00001", "P1", "c3-b", "20261109T100000.000Z", "test", "0")
    assert sorted([it1, it0], key=Q.plan_key)[0]["tier"] == "P0"


def test_pc_tier_after_c3b_items():
    def it(tier, dev, ev, ref):
        return Q._plan_item("window", ref, tier, dev, "20261110T100000.000Z", "train", ev)
    plan = [it("P1", "c3-b", "0", "a"), it("P3", "c3-b", "1", "b"), it("P3", "c3-a", "1", "c"),
            it("P3", "c3-b", "0", "d"), it("P3", "c3-a", "0", "e"), it("P4", "c3-a", "0", "f")]
    out = K.insert_tier(plan, [it("PC", "c3-a", "0", "x"), it("PC", "c3-b", "1", "y")])
    assert [p["ref"] for p in out] == ["a", "b", "c", "d", "x", "y", "e", "f"]
    assert sorted(out, key=Q.plan_key)[4]["tier"] == "PC" or Q.plan_key(out[4]) < Q.plan_key(out[6])
    assert [p["ref"] for p in K.insert_tier(plan, [it("P0", "c3-a", "0", "z")])][0] == "z"
    again = K.insert_tier(out, [it("PC", "c3-b", "0", "w")])                      # after the earlier PC items
    assert [p["ref"] for p in again][4:8] == ["x", "y", "w", "e"]
