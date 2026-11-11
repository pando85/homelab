"""Fast pure tests of the v6 context-clip planning and assembly (no Piper, no audio files)."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import v6_gen_ctx as g  # noqa: E402

RATE = g.RATE
CTX, BASE = g.load_plan()


def nspk(stem):
    return 5


def items(set_name, n=600, seed=6):
    return [g.plan_item(CTX, BASE, set_name, i, seed, nspk) for i in range(n)]


def test_plan_is_deterministic_and_independent_of_order():
    a = g.plan_item(CTX, BASE, "ctx_pos_train", 17, 6, nspk)
    assert a == g.plan_item(CTX, BASE, "ctx_pos_train", 17, 6, nspk)
    assert a != g.plan_item(CTX, BASE, "ctx_pos_train", 18, 6, nspk)
    assert a != g.plan_item(CTX, BASE, "ctx_pos_train", 17, 7, nspk)


def test_train_voice_filter_and_heldout_roles():
    train = {it["voice"] for it in items("ctx_pos_train") + items("ctx_neg_train")}
    assert not train & set(CTX["exclude_train_voices"])
    assert {"ald", "antton", "ryan", "l2arctic_tr", "semaine", "thorsten_emo", "kristin", "cori", "south_f",
            "mihai", "tugao"} == set(CTX["exclude_train_voices"])
    held = {k for k, v in BASE["voices"].items() if v[1] == "heldout"}
    ev = {it["voice"] for it in items("ctx_pos_eval", 200) + items("ctx_neg_eval", 200)}
    assert ev == held
    assert train and not train & held


def test_ctx_pos_structure():
    its = items("ctx_pos_train", 1500)
    assert {it["kind"] for it in its} == {"ctx_pos"}
    assert {it["joiner"] for it in its} <= {"space", "comma", "together"}
    ph = [p for it in its for m, p in it["head"] if m == "ph"]
    assert len(ph) == len(its)  # exactly one target phoneme piece, never a second "oye"/"kivi" in the prefix
    assert all(not p.endswith((".", "!", "?")) for p in ph)
    for it in its:
        low = it["prefix"].lower()
        assert "oye" not in low and "oiga" not in low and "kivi" not in low
        assert it["head"][-1][0] == "ph"
        assert len(it["head"]) == 1 + int(bool(it["prefix"]))
        assert it["has_prefix"] == int(bool(it["prefix"])) and it["prefix_words"] == len(it["prefix"].split())
        assert it["prefix_words"] <= 2
        assert 0.75 <= it["length_scale"] <= 1.2
        assert it["has_command"] == int(bool(it["tail"]))
        if not it["has_command"]:
            assert it["has_prefix"] and it["gap_s"] is None
        else:
            assert 0.0 <= it["gap_s"] <= 0.12
            assert it["tail"] in CTX["commands"]
    no_cmd = np.mean([not it["has_command"] for it in its])
    assert 0.11 < no_cmd < 0.19
    pref = np.mean([it["has_prefix"] for it in its])
    assert 0.5 < pref < 0.65
    gaps = np.array([it["gap_s"] for it in its if it["has_command"]])
    assert np.mean(gaps < 0.06) > 0.6 and gaps.max() <= 0.12


def test_ctx_neg_mix_and_confusers():
    its = items("ctx_neg_train", 3000)
    cnt = {k: np.mean([it["subtype"] == k for it in its]) for k in ("confuser", "kivi_sentence", "plain")}
    assert abs(cnt["confuser"] - 0.6) < 0.04 and abs(cnt["kivi_sentence"] - 0.3) < 0.04 and abs(cnt["plain"] - 0.1) < 0.03
    conf = [it["variant"] for it in its if it["subtype"] == "confuser"]
    assert set(conf) == set(CTX["confusers"])
    assert conf.count("oye kili") > conf.count("oye kiri") > conf.count("oye tú")
    for it in its:
        if it["subtype"] == "confuser":
            assert it["variant"] in [t for m, t in it["head"]]
            assert it["focus"] and it["joiner"] == ""
        elif it["subtype"] == "kivi_sentence":
            assert "oye" not in it["variant"].lower()
            assert "kiv" in it["variant"].lower() or "kiwi" in it["variant"].lower()
            assert "|" not in it["variant"] and it["head"][0][0] == "text"
            assert it["has_command"] == int(bool(it["tail"]))
        else:
            assert it["subtype"] == "plain" and not it["focus"] and it["has_command"] == 1 and it["tail"] == ""
            assert it["command"] in CTX["commands"]


def test_kivi_sentences_split_and_content():
    heads = {s.split("|")[0] for s in CTX["kivi_sentences"]}
    assert len(CTX["kivi_sentences"]) >= 12
    assert all(s.count("|") == 1 for s in CTX["kivi_sentences"])
    assert all(("kivi" in h.lower() or "kiwi" in h.lower()) for h in heads)
    assert all("oye" not in s.lower() for s in CTX["kivi_sentences"])
    assert len(CTX["commands"]) >= 40
    assert not any(" oye" in " " + p or "kivi" in p for p in CTX["prefixes"])


def test_assemble_boundaries_exact():
    head = np.full(8000, 0.5, np.float32)
    tail = np.full(4000, 0.1, np.float32)
    xi, info = g.assemble(head, tail, 0.05, 0.0, -6.0)
    assert info["phrase_start"] == 0 and info["phrase_end"] == 8000
    assert info["cmd_start"] == 8000 + 800 and info["total_samples"] == 8000 + 800 + 4000 == len(xi)
    assert np.all(xi[8000:8800] == 0)
    assert np.all(xi[8800:] != 0) and np.all(xi[:8000] != 0)
    assert xi.dtype == np.int16
    assert abs(int(np.max(np.abs(xi))) - 32767 * 10 ** (-6 / 20)) <= 1
    # command level follows cmd_rel_db against the head
    xi2, _ = g.assemble(head, tail, 0.0, -6.0, -1.0)
    ratio = np.abs(xi2[8000:]).max() / np.abs(xi2[:8000]).max()
    assert abs(20 * np.log10(ratio) + 6.0) < 0.1


def test_assemble_no_command_and_zero_gap():
    head = np.sin(np.linspace(0, 200, 6400)).astype(np.float32)
    xi, info = g.assemble(head, None, None, 0.0, -3.0)
    assert info["cmd_start"] is None and info["phrase_end"] == len(xi) == 6400
    xi, info = g.assemble(head, head[:1600], 0.0, 0.0, -3.0)
    assert info["cmd_start"] == info["phrase_end"] == 6400 and len(xi) == 8000


def test_drop_rules():
    ok = np.ones(16000, np.int16)
    pos = {"kind": "ctx_pos", "subtype": "target", "prefix_words": 0}
    assert g.drop_reason(CTX, pos, int(1.5 * RATE), ok) is None
    assert g.drop_reason(CTX, pos, int(1.5 * RATE) + 1, ok) == "target_too_long"
    pre2 = {**pos, "prefix_words": 2}
    assert g.drop_reason(CTX, pre2, int(2.7 * RATE), ok) is None
    assert g.drop_reason(CTX, pre2, int(2.7 * RATE) + 1, ok) == "utt_too_long"
    # sentences and plain commands have no length rule, but the zero rule applies to everything
    sent = {"kind": "ctx_neg", "subtype": "kivi_sentence", "prefix_words": 0}
    assert g.drop_reason(CTX, sent, 5 * RATE, ok) is None
    z = np.ones(1000, np.int16)
    z[:201] = 0
    assert g.drop_reason(CTX, sent, 1000, z) == "zeros"
    z[:200] = 0
    z[200] = 1
    assert g.drop_reason(CTX, sent, 1000, z) is None


def test_manifest_row_columns_and_blanks():
    it = g.plan_item(CTX, BASE, "ctx_neg_train", 3, 6, nspk)
    info = {"phrase_start": 0, "phrase_end": 100, "cmd_start": None, "total_samples": 100, "head_samples": 100,
            "phonemes": "x", "zero_share": 0.0}
    row = g.manifest_row(it, info)
    assert list(row) == g.MANIFEST_COLS
    plain = next(g.plan_item(CTX, BASE, "ctx_neg_train", i, 6, nspk) for i in range(400)
                 if g.plan_item(CTX, BASE, "ctx_neg_train", i, 6, nspk)["subtype"] == "plain")
    r = g.manifest_row(plain, {**info, "cmd_start": None})
    assert r["phrase_end"] == "" and r["phrase_start"] == ""


def synth(speech_s=0.8, gap_s=0.1, cmd_s=0.8, seed=0):
    rng = np.random.default_rng(seed)

    def burst(n):
        return (rng.standard_normal(n) * 0.2 * np.hanning(n) ** 0.2).astype(np.float32)
    head, tail = burst(int(speech_s * RATE)), burst(int(cmd_s * RATE))
    head[-1600:] *= np.linspace(1, 0.002, 1600)
    xi, info = g.assemble(head, tail, gap_s, 0.0, -3.0)
    return xi, info


def test_boundary_check_finds_dip_and_onset():
    xi, info = synth(gap_s=0.1)
    dip, onset = g.boundary_check(xi, info["phrase_end"], info["cmd_start"])
    assert dip and onset
    # a boundary placed in the middle of continuous speech shows neither a dip nor an onset
    dip, onset = g.boundary_check(xi, 4000, None)
    assert not dip and not onset
    # no command: the clip ends in a fade, still a dip near the end
    xi, info = synth()
    xi = xi[:info["phrase_end"]]
    assert g.boundary_check(xi, info["phrase_end"], None)[0]


def test_frame_db_levels():
    x = np.full(RATE, 0.5, np.float32)
    db, t = g.frame_db(x)
    assert abs(db[0] - 20 * np.log10(0.5)) < 0.01 and len(db) == len(t) == 99
    db16, _ = g.frame_db((x * 32767).astype(np.int16))
    assert abs(db16[0] - db[0]) < 0.01


@pytest.mark.parametrize("set_name", g.SETS)
def test_default_counts_and_roles(set_name):
    assert CTX["default_n"][set_name] in (5000, 800)
    assert g.set_role(set_name) == ("heldout" if set_name.endswith("_eval") else "train")
