import json
import random

import pytest

import v6p3r_decoys as D


@pytest.mark.parametrize("text", ["oye Kivi", "oye kiwi", "oye, quivi", "oye kibi enciende la luz", "oye compra kiwis",
                                  "bueno oye Kivi", "oye tú Kivi"])
def test_phrase_check_drops_the_wake_phrase_and_homophones(text):
    ok, why = D.phrase_check(text)
    assert not ok and why


@pytest.mark.parametrize("text", ["oye mira", "oye Kevin", "oye quizá", "Kevin no viene hoy", "el kiwi está verde",
                                  "oye Kiko enciende la luz"])
def test_phrase_check_keeps_decoys(text):
    assert D.phrase_check(text) == (True, "")


def test_hand_lists_hold_no_wake_phrase():
    oye_other, near, sent, dropped = D.clean_lists()
    assert dropped == []
    for k in oye_other:
        assert D.phrase_check(f"oye {k}")[0]
    for k in near:
        assert D.phrase_check(f"oye {k}")[0]
    for s in sent:
        assert "|" in s and D.phrase_check(s.replace("|", " "))[0]


def test_category_of_evidence_flags():
    assert D.category_of({"ph": "1", "oye": "1", "kivi": "1"}) is None
    assert D.category_of({"ph": "0", "oye": "1", "weak": "0", "kivi": "0", "ksim": "0.4"}) == "oye_other"
    assert D.category_of({"ph": "0", "oye": "1", "weak": "1", "kivi": "0", "ksim": "0.75"}) == "oye_near"
    assert D.category_of({"ph": "0", "oye": "0", "weak": "0", "kivi": "0", "ksim": "0.8"}) == "near_no_oye"
    assert D.category_of({"ph": "0", "oye": "0", "ksim": "0.2", "words": "5"}) == "speech"
    assert D.category_of({"ph": "0", "oye": "0", "ksim": "0", "words": "0"}) == "none"


def test_weights_from_shares_floor_and_sum():
    w = D.weights_from_shares({"oye_other": 20, "oye_near": 0, "near_no_oye": 5, "speech": 1000})
    assert set(w) == set(D.CATEGORIES)
    assert abs(sum(w.values()) - 1) < 1e-3
    assert min(w.values()) >= D.WEIGHT_FLOOR / (1 + 2 * D.WEIGHT_FLOOR) - 1e-3
    assert w["oye_other"] > w["near_sentence"] > w["oye_near"]
    assert D.weights_from_shares({}) == D.DEFAULT_WEIGHTS


def test_plan_item_is_deterministic_and_never_the_phrase():
    ctx, base = D._plan()
    oye_other, near, sent, _ = D.clean_lists()
    phrases = {"weights": D.DEFAULT_WEIGHTS, "oye_other": oye_other, "oye_near": near, "near_sentence": sent,
               "chat": D.CHAT}
    nspk = lambda stem: 4
    a = [D.plan_item(i, "train", ctx, base, phrases, nspk) for i in range(300)]
    b = [D.plan_item(i, "train", ctx, base, phrases, nspk) for i in range(300)]
    assert a == b
    assert {it["category"] for it in a} == set(D.CATEGORIES)
    assert not any(it["voice"] in D.DROP_VOICES for it in a)
    for it in a:
        assert D.phrase_check(it["text"])[0], it["text"]
        assert it["kind"] == "ctx_neg" and it["head"]
    ev = [D.plan_item(i, "eval", ctx, base, phrases, nspk) for i in range(50)]
    pool = D.voice_pool(ctx, base, "heldout")
    assert all(it["voice"] in pool for it in ev)


def test_manifest_row_format():
    it = {"idx": 1, "id": "decoy_train_000001", "file": "decoy_train_000001.wav", "set": "train", "category": "oye_other",
          "variant": "mira", "text": "oye mira", "voice": "davefx", "model": "es_ES-davefx-medium", "speaker_id": None,
          "lang": "es", "length_scale": 1.0, "noise_scale": 0.5, "noise_w_scale": 0.5, "warp": 1.0, "peak_db": -3.0,
          "gap_s": None, "cmd_rel_db": 0.0, "has_command": 0}
    row = D.manifest_row(it, {"head_samples": 8000, "cmd_start": None, "total_samples": 8000, "zero_share": 0.0}, "ab")
    assert {"id", "category", "text", "voice", "speed", "aug", "duration_s", "sha256", "phrase_end"} <= set(row)
    assert row["duration_s"] == 0.5 and row["kind"] == "ctx_neg"
    json.dumps(row)


def test_head_limit():
    assert D.head_limit_s({"head_words": 2}) == D.MAX_HEAD2_S
    assert D.head_limit_s({"head_words": 4}) == pytest.approx(D.MAX_HEAD2_S + 2 * D.PER_WORD_S)
    random.seed(0)
