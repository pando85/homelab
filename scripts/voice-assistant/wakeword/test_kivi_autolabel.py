"""Tests of kivi_autolabel.py and `kivi_live_wakes.py autolabel`. Synthetic strings, signals and embeddings only,
fake transcriber / embedder / scorer: no model is loaded, no real clip is read.

    cd scripts/voice-assistant/wakeword && OMP_NUM_THREADS=4 \
        /work/workspaces/kivi-wakeword/.venv/bin/python -m pytest test_kivi_autolabel.py -q
"""
import os
import sys
from argparse import Namespace
from pathlib import Path

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "4")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kivi_autolabel as A  # noqa: E402
import kivi_live_wakes as L  # noqa: E402
import kivi_real_common as C  # noqa: E402

RATE = C.RATE


def W(*texts, t0=0.0, step=0.4, prob=0.9):
    return [(t, t0 + i * step, t0 + i * step + 0.35, prob) for i, t in enumerate(texts)]


# ---------------------------------------------------------------------------------------------
# text matching on synthetic strings
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("words", [
    ["Oye,", "Kivi"], ["oye", "kiwi"], ["Oye", "Quivi."], ["oy", "kibi"], ["Oye", "ki", "vi"], ["Oyé", "KIVÍ!"],
    ["hoye", "kivi"], ["olle", "kivi"], ["oyekivi"], ["oye", "oye", "kivi"], ["Oye", "kivy"],
])
def test_phrase_variants_match(words):
    m = A.match_words(W(*words))
    assert m["phrase"] and m["phrase_score"] >= 0.8 and m["span"] is not None


@pytest.mark.parametrize("words", [
    ["enciende", "la", "luz"], [], ["kivi"], ["oye"], ["oye", "kili"], ["oye", "tivi"], ["hola", "kivi", "oye"],
    ["oye", "a", "b", "c", "kivi"],
])
def test_non_phrases_do_not_match(words):
    assert not A.match_words(W(*words))["phrase"]


def test_kivi_alone_and_near_flags():
    m = A.match_words(W("kiwi"))
    assert m["kivi_exact"] and not m["phrase"] and not m["oye_like"]
    m = A.match_words(W("oye", "kili"))
    assert m["weak"] and m["oye_like"] and not m["kivi_exact"]
    m = A.match_words(W("la", "casa"))
    assert not (m["kivi_near"] or m["oye_like"]) and m["n_words"] == 2


def test_span_is_phrase_timing_only():
    m = A.match_words([("zzhola", 0.0, 0.3, 0.9), ("oye", 1.0, 1.3, 0.9), ("kivi", 1.35, 1.8, 0.9), ("zzluz", 1.9, 2.2, 0.9)])
    assert m["span"] == (1.0, 1.8)
    assert "zzhola" not in str(m) and "zzluz" not in str(m)             # evidence holds no text


def test_fold_and_sim():
    assert A.fold("Quivi") == A.fold("kiwi") == A.fold("KIVÍ") == "kibi"
    assert A.sim("kibi", "kibi") == 1.0 and A.sim("kili", "kibi") == 0.75 and A.sim("", "x") == 0.0


def test_combine_matches():
    full, last = A.match_words(W("oye", "kivi")), A.match_words(W("nada"))
    c = A.combine_matches(full, last)
    assert c["phrase"] and not c["phrase_both"] and c["span"] is not None
    c2 = A.combine_matches(full, full)
    assert c2["phrase_both"]
    assert not A.combine_matches(last, None)["phrase"]


# ---------------------------------------------------------------------------------------------
# label rules
# ---------------------------------------------------------------------------------------------


def ev(words, **kw):
    d = dict(A.combine_matches(A.match_words(W(*words)), None), speech_last3_s=2.0, stt_len=3.0, own_peak=0.0, own_thr=2.0)
    d.update(kw)
    return d


def test_decide_label():
    assert A.decide_label(ev(["oye", "kivi"])) == ("true", 0.92)
    assert A.decide_label(ev(["oye", "kivi"], own_peak=0.99, own_thr=0.97, phrase_both=True)) == ("true", 0.98)
    assert A.decide_label(ev(["oye", "kili"]))[0] == "unclear"
    assert A.decide_label(ev(["kivi"]))[0] == "unclear"
    assert A.decide_label(ev([], speech_last3_s=0.0)) == ("false", 0.95)
    assert A.decide_label(ev([], speech_last3_s=1.5))[0] == "unclear"
    assert A.decide_label(ev(["pon", "la", "tele"], stt_len=1.0)) == ("false", 0.8)
    assert A.decide_label(ev(["pon", "la", "tele"], mean_prob=0.9)) == ("false", 0.75)
    assert A.decide_label(ev(["pon", "la", "tele"], mean_prob=0.3))[0] == "unclear"


def test_is_uncertain():
    base = {"label": "", "label_source": "", "label_conf": "", "speaker": ""}
    assert A.is_uncertain(base)
    owner = dict(base, label="true")
    assert not A.is_uncertain(owner)
    auto = dict(base, label="true", label_source="auto-v1", label_conf="0.92", speaker="spk01")
    assert not A.is_uncertain(auto)
    assert A.is_uncertain(dict(auto, speaker="unknown")) and A.is_uncertain(dict(auto, label_conf="0.7"))
    assert A.is_uncertain(dict(auto, label="unclear", label_conf="0.5"))
    assert not A.is_uncertain(dict(auto, label="false", label_conf="0.95", speaker="unknown"))
    assert not A.is_uncertain(dict(auto, label="not-a-wake", label_conf="0.9", speaker="unknown"))


# ---------------------------------------------------------------------------------------------
# trigger, phrase end, continuous command
# ---------------------------------------------------------------------------------------------


def test_trigger_of():
    thr = A.MODEL_SPECS["oye_kivi"][1]
    pk = lambda v: {"oye_kivi": (v, v, v, 5.9, v)}                                        # noqa: E731
    assert A.trigger_of("oye_kivi", "", pk(thr + 0.01), 6.0) == ("wake", "same", "cross")
    assert A.trigger_of("oye_kivi", "", pk(thr - 0.001), 6.0) == ("unknown", "same", "weak")   # no near-miss class: exact replay
    assert A.trigger_of("oye_kivi", "", pk(thr - 0.05), 6.0) == ("unknown", "same", "weak")
    assert A.trigger_of("oye_kivi", "", pk(0.7), 6.0) == ("unknown", "same", "weak")
    assert A.trigger_of("oye_kivi", "Kiwi/12", pk(0.01), 6.0) == ("button", "sep", "none")
    assert A.trigger_of("oye_kivi", "", pk(0.01), 0.5)[0] == "unknown"                 # tail too short
    assert A.trigger_of("nope", "", pk(0.99), 6.0)[0] == "unknown"


def burst(sec_total, a, b, amp=8000, seed=0):
    r = np.random.default_rng(seed)
    x = (r.standard_normal(int(sec_total * RATE)) * 30).astype(np.float32)
    x[int(a * RATE):int(b * RATE)] += (r.standard_normal(int((b - a) * RATE)) * amp).astype(np.float32)
    return x.astype(np.int16)


def test_continuous_after():
    x = burst(4.0, 1.0, 3.0)                                                           # speech 1-3 s
    assert A.continuous_after(x, 1.5) is True                                          # speech goes on
    assert A.continuous_after(x, 3.2) is False                                         # silence after
    assert A.continuous_after(x[:int(1.5 * RATE)], 1.45) is None                       # audio ends before the window
    assert A.speech_seconds(x) > 1.5 and A.speech_seconds(np.zeros(RATE * 3, dtype=np.int16)) == 0.0


# ---------------------------------------------------------------------------------------------
# speaker
# ---------------------------------------------------------------------------------------------


def synth_items(n1=30, n2=12, noise=0.12, seed=1):
    r = np.random.default_rng(seed)
    d = 16
    c1, c2 = np.eye(d)[0], np.eye(d)[1]
    items = [("spk01", A.unit(c1 + noise * r.standard_normal(d))) for _ in range(n1)]
    items += [("spk02", A.unit(c2 + noise * r.standard_normal(d))) for _ in range(n2)]
    return items, d


def test_calibrate_and_classify():
    items, d = synth_items()
    thr, rep = A.calibrate(items, groups=["g%d" % (i % 3) for i in range(len(items))])
    assert rep["n"] == 42 and rep["correct"] >= 38 and rep["wrong"] <= 1
    cen = A.centroids(items)
    assert A.classify(A.sims_to(cen, np.eye(d)[0]), thr)[0] == "spk01"
    assert A.classify(A.sims_to(cen, np.eye(d)[1]), thr)[0] == "spk02"
    mid = A.unit(np.eye(d)[0] + np.eye(d)[1])                                         # equally close: low margin
    assert A.classify(A.sims_to(cen, mid), thr)[0] in ("unknown", "spk-other")      # never one of the two
    far = A.unit(np.eye(d)[5])
    assert A.classify(A.sims_to(cen, far), thr)[0] in ("spk-other", "unknown")
    assert A.classify({}, thr) == ("unknown", 0.0)
    assert 0.5 <= A.classify(A.sims_to(cen, np.eye(d)[0]), thr)[1] <= 0.99


# ---------------------------------------------------------------------------------------------
# merge: owner labels win, nothing but empty cells is written
# ---------------------------------------------------------------------------------------------


def blank(run, **kw):
    return {k: "" for k in L.FIELDS} | {"run_id": run, "device": "c3-a", "utc": "20261109T100000.000Z"} | kw


def res(label="true", conf=0.92, speaker="spk01", trig="wake", **kw):
    return {"label": label, "label_conf": conf, "speaker": speaker, "speaker_conf": 0.8, "trigger": trig,
            "evidence": "ph=1", "phrase_end_s": "4.20", "continuous_command": "true", "phrase_in_stt": "false",
            "stt_phrase_start_s": "", "stt_phrase_end_s": "", **kw}


def test_merge_never_overwrites_owner_cells():
    rows = [blank("a"), blank("b", label="false", speaker="spk02"), blank("c", label="true"),
            blank("d", label="true", speaker="spk01", label_source="owner")]
    n = A.merge_results(rows, {k: res() for k in "abcd"})
    a, b, c, d = rows
    assert (a["label"], a["label_source"], a["label_conf"], a["speaker"], a["speaker_conf"]) == ("true", "auto-v1", "0.92", "spk01", "0.80")
    assert (b["label"], b["label_source"], b["speaker"]) == ("false", "", "spk02")      # the owner's cells stay
    assert (c["label"], c["label_source"], c["speaker"]) == ("true", "", "spk01")       # only the empty speaker is filled
    assert (d["label"], d["label_source"], d["speaker"]) == ("true", "owner", "spk01")
    assert n["label"] == 1 and n["label_owner_kept"] == 3
    # a second run changes nothing
    before = [dict(r) for r in rows]
    A.merge_results(rows, {k: res(label="false") for k in "abcd"})
    assert rows == before


def test_merge_redo_only_recomputes_auto_cells():
    rows = [blank("a"), blank("b", label="true", speaker="spk01")]
    A.merge_results(rows, {"a": res(), "b": res()})
    A.merge_results(rows, {"a": res(label="unclear", conf=0.5, speaker="unknown"), "b": res(label="false")}, redo=True)
    assert rows[0]["label"] == "unclear" and rows[0]["label_conf"] == "0.50"
    assert rows[1]["label"] == "true" and rows[1]["speaker"] == "spk01"


def test_button_rows_and_conflicts():
    rows = [blank("a"), blank("b", label="true", speaker="spk01")]
    r = res(label="not-a-wake", conf=0.9, speaker="unknown", trig="button")
    n = A.merge_results(rows, {"a": r, "b": r})
    assert rows[0]["label"] == "not-a-wake" and rows[0]["speaker"] == "unknown" and rows[0]["trigger"] == "button"
    assert rows[1]["label"] == "true" and "conflict=owner-label-vs-button" in rows[1]["auto_evidence"] and n["conflict"] == 1


def test_false_label_forces_unknown_speaker():
    rows = [blank("a")]
    A.merge_results(rows, {"a": res(label="false", speaker="spk01")})
    assert rows[0]["speaker"] == "unknown"


def test_agreement_counts_owner_cells_only():
    rows = [blank("a", label="true", speaker="spk01"), blank("b", label="false"), blank("c", label="true"),
            blank("d", label="true", label_source="auto-v1"), blank("e")]
    results = {"a": res(), "b": res(label="unclear", speaker="unknown"), "c": res(label="false"), "d": res(), "e": res()}
    ag = A.agreement(rows, results)
    assert ag["owner_labels"] == 3 and ag["label_agree"] == 1 and ag["label_auto_unclear"] == 1 and ag["label_disagree"] == 1
    assert ag["owner_speakers"] == 1 and ag["speaker_agree"] == 1


def test_wait_for_review():
    states = iter([True, True, False])
    slept = []
    assert A.wait_for_review(lambda: next(states), sleep=slept.append, out=lambda *_: None) and slept == [30, 30]
    assert not A.wait_for_review(lambda: True, sleep=lambda s: None, max_s=60, out=lambda *_: None)


# ---------------------------------------------------------------------------------------------
# end to end with fakes
# ---------------------------------------------------------------------------------------------


class FakeTranscriber:
    """Chooses the words by the first sample of the audio (a marker written into the clip)."""

    def __init__(self, table):
        self.table = table
        self.calls = 0

    def transcribe(self, pcm):
        self.calls += 1
        return self.table.get(int(pcm[0]) if len(pcm) else 0, [])


class FakeEmbedder:
    def embed(self, pcm):
        d = 16
        v = np.zeros(d)
        v[0 if int(pcm[1]) < 100 else 1] = 1.0
        return A.unit(v + 0.01 * np.random.default_rng(len(pcm)).standard_normal(d))

    def close(self):
        pass


class FakeScorer:
    def __init__(self, by_marker):
        self.by = by_marker

    def peaks(self, pcm, n_total=None):
        self.n_total = n_total
        v = self.by.get(int(pcm[0]), 0.0)
        return {"oye_kivi": (v, v, v, 5.5, v), "oye_kivi_next": (v, v, v, 5.5, v)}


def write_clip(live, dev, utc, marker, spk_level, dur=6.0):
    x = burst(dur, 1.0, dur - 0.5)
    x[0], x[1] = marker, spk_level
    d = live / dev / utc
    d.mkdir(parents=True)
    C.write_wav_new(d / "wake_tail.wav", x)
    s = burst(3.0, 0.2, 2.5, seed=2)
    s[0], s[1] = marker, spk_level
    C.write_wav_new(d / "stt.wav", s)


def make_dataset(tmp_path):
    root = tmp_path / "ds"
    live = root / "live"
    live.mkdir(parents=True)
    spec = [("c3-a", "20261109T100000.000Z", 11, 10, ""), ("c3-a", "20261109T110000.000Z", 12, 10, ""),
            ("c3-b", "20261110T100000.000Z", 13, 500, "Kiwi/1"), ("c3-b", "20261110T110000.000Z", 14, 10, ""),
            ("c3-b", "20261110T120000.000Z", 15, 10, "")]
    rows = []
    for dev, utc, mk, lvl, stt_run in spec:
        write_clip(live, dev, utc, mk, lvl)
        rows.append({k: "" for k in L.FIELDS} | {"run_id": f"Kiwi/{mk}", "device": dev, "model": "oye_kivi_next", "utc": utc,
                                                  "wake_len": "6.0", "stt_len": "3.0", "bucket": "suspect-true",
                                                  "stt_run": stt_run})
    rows[4]["stt_len"] = "1.0"
    rows[4]["label"] = "true"                                                          # an owner label, present at the start
    rows[4]["speaker"] = "spk01"
    L.write_table(live / "live_wakes.csv", rows)
    return root


def test_autolabel_end_to_end_with_fakes(tmp_path, monkeypatch):
    root = make_dataset(tmp_path)
    table = root / "live" / "live_wakes.csv"
    items, _ = synth_items(8, 6, noise=0.05)
    items = [(s, v) for s, v in items]
    a = Namespace(dataset_root=str(root), dry_run=False, redo=False, work="/nonexistent", pgrep=lambda: False,
                  enrol=[], wait_kw={})
    tr = FakeTranscriber({
        11: W("oye", "kivi", "enciende", "la", "luz"), 12: W("buenas", "tardes", "a", "todos"),
        13: W("pon", "musica"), 14: [], 15: W("oye", "kiwi", t0=0.5)})
    sc = FakeScorer({11: 0.99, 12: 0.99, 13: 0.0, 14: 0.99, 15: 0.99})
    monkeypatch.setattr(A, "enrolment_clips", lambda r: [])
    monkeypatch.setattr(A, "enrol", lambda clips, emb, max_len_s=4.0: (items, ["g"] * len(items)))
    out = []
    L.cmd_autolabel(a, out=out.append, transcriber=tr, embedder=FakeEmbedder(), scorer=sc)
    rows = {r["run_id"]: r for r in L.read_table(table)}
    r11, r12, r13, r14, r15 = (rows[f"Kiwi/{m}"] for m in (11, 12, 13, 14, 15))
    assert (r11["label"], r11["label_source"], r11["trigger"]) == ("true", "auto-v1", "wake")
    assert r11["speaker"] == "spk01" and float(r11["speaker_conf"]) > 0.5
    assert r11["phrase_end_s"] and r11["continuous_command"] in ("true", "false")
    assert r12["label"] == "false" and r12["speaker"] == "unknown"                  # unrelated words, wake (but 0.75)
    assert (r13["label"], r13["trigger"], r13["speaker"]) == ("not-a-wake", "button", "unknown")
    assert r13["stt_run"] == "Kiwi/1"
    assert r14["label"] == "false" and float(r14["label_conf"]) == 0.95             # no words, burst of noise... see below
    assert r15["label"] == "true" and r15["label_source"] == "" and r15["speaker"] == "spk01"     # owner cells intact
    assert r15["trigger"] == "wake" and "label_source" in r15
    # no row evidence holds any of the synthetic words
    text = table.read_text()
    for word in ("enciende", "buenas", "musica", "kiwi", "tardes"):
        assert word not in text
    assert any("agreement with owner cells" in o for o in out)
    assert list(table.parent.glob("live_wakes.csv.bak-*"))
    assert not any(w in " ".join(out) for w in ("enciende", "buenas", "musica"))


def test_autolabel_refuses_to_write_while_review_runs(tmp_path, monkeypatch):
    root = make_dataset(tmp_path)
    table = root / "live" / "live_wakes.csv"
    before = table.read_text()
    a = Namespace(dataset_root=str(root), dry_run=False, redo=False, work="/x", pgrep=lambda: True, enrol=[],
                  wait_kw={"sleep": lambda s: None, "max_s": 60})
    items, _ = synth_items(8, 6, noise=0.05)
    monkeypatch.setattr(A, "enrol", lambda clips, emb, max_len_s=4.0: (items, ["g"] * len(items)))
    with pytest.raises(SystemExit):
        L.cmd_autolabel(a, out=lambda *_: None, transcriber=FakeTranscriber({}), embedder=FakeEmbedder(),
                        scorer=FakeScorer({}))
    assert table.read_text() == before and not list(table.parent.glob("*.bak-*"))


# ---------------------------------------------------------------------------------------------
# exact replay: tail length from wake_len, trigger-only pass
# ---------------------------------------------------------------------------------------------


def test_wake_total_from_wake_len():
    assert A.wake_total({"wake_len": "13.12"}, 96000) == 209920
    assert A.wake_total({"wake_len": "6.00"}, 96000) == 96000
    assert A.wake_total({"wake_len": "3.5"}, 96000) == 96000          # shorter than the tail: the tail itself
    assert A.wake_total({"wake_len": ""}, 96000) == 96000 and A.wake_total({}, 5) == 5
    assert A.wake_total({"wake_len": "abc"}, 5) == 5


def test_evidence_set_keeps_everything_else():
    ev = "ph=1;v4=0.1;own2=0.2;pe_src=model;pe_ok=;trig=wake/same/cross;sp3=1.0;conflict=owner-label-vs-button"
    out = A.evidence_set(ev, {"v4": "0.9", "trig": "button/sep/none", "new": "1"}, drop=("conflict",))
    assert out == "ph=1;v4=0.9;own2=0.2;pe_src=model;pe_ok=;trig=button/sep/none;sp3=1.0;new=1"
    assert A.evidence_get(ev, "pe_src") == "model" and A.evidence_get(ev, "pe_ok") == "" and A.evidence_get(ev, "zz") is None
    assert A.evidence_set("", {"a": 1}) == "a=1"


def trow(run, ev="ph=0;v4=0.0;v5=0.0;own2=0.0;pe_src=;pe_ok=;trig=button/sep/none;sp3=0.0", **kw):
    return blank(run, model="oye_kivi_next", auto_evidence=ev, **kw)


def tres(trig="wake", **kw):
    return {"trigger": trig, "cross": ("same", "cross" if trig == "wake" else "none"), "trig": f"{trig}/same/x", "t_model": 5.5,
            "peak_t_s": "5.50", "peak_raw": "0.9900", "v4": "0.5", "v5": "0.99", "own2": "0.99", "phrase_end_s": "5.50",
            "continuous_command": "true"} | kw


def test_merge_trigger_only_changes_model_parts_only():
    rows = [
        trow("auto_btn", label="not-a-wake", label_source="auto-v1", label_conf="0.90", speaker="unknown", trigger="button",
             phrase_end_s="", continuous_command=""),                                         # relabel: button no more
        trow("owner_btn", label="false", speaker="spk01", trigger="wake"),                    # owner row, now button: conflict
        trow("owner_ok", ev="ph=1;trig=wake/same/cross;conflict=owner-label-vs-button", label="true", speaker="spk01",
             trigger="button"),                                                               # stale conflict dropped
        trow("model_pe", ev="ph=0;v4=0.1;v5=0.2;own2=0.3;pe_src=model;pe_ok=;trig=wake/same/near", label="true", label_source="auto-v1",
             label_conf="0.92", speaker="spk01", trigger="wake", phrase_end_s="4.00", continuous_command="false"),
        trow("words_pe", ev="ph=1;pe_src=words;pe_ok=1;trig=wake/same/near", label="true", label_source="auto-v1", label_conf="0.92",
             speaker="spk01", trigger="wake", phrase_end_s="4.00", continuous_command="false"),
        trow("noev", ev="", trigger=""),
        trow("untouched"),
    ]
    before = {r["run_id"]: dict(r) for r in rows}
    results = {"auto_btn": tres("wake"), "owner_btn": tres("button"), "owner_ok": tres("wake"), "model_pe": tres("wake"),
               "words_pe": tres("wake"), "noev": tres("wake")}
    n = A.merge_trigger_only(rows, results)
    by = {r["run_id"]: r for r in rows}
    for r in rows:                                                                         # labels and speakers never change
        b = before[r["run_id"]]
        assert all(r[k] == b[k] for k in ("label", "label_source", "label_conf", "speaker", "split", "stt_run", "sha256"))
    a = by["auto_btn"]
    assert a["trigger"] == "wake" and a["peak_t_s"] == "5.50" and a["peak_raw"] == "0.9900"
    assert a["auto_evidence"].endswith(";relabel=trigger-changed") and "trig=wake/same/x" in a["auto_evidence"]
    assert a["phrase_end_s"] == ""                                                         # pe_src was empty: untouched
    o = by["owner_btn"]
    assert o["trigger"] == "button" and "conflict=owner-label-vs-button" in o["auto_evidence"] and "relabel" not in o["auto_evidence"]
    assert "conflict" not in by["owner_ok"]["auto_evidence"] and by["owner_ok"]["trigger"] == "wake"
    m = by["model_pe"]
    assert m["phrase_end_s"] == "5.50" and m["continuous_command"] == "true"
    assert m["auto_evidence"] == "ph=0;v4=0.5;v5=0.99;own2=0.99;pe_src=model;pe_ok=;trig=wake/same/x"
    w = by["words_pe"]
    assert w["phrase_end_s"] == "4.00" and w["continuous_command"] == "false" and "pe_ok=1" in w["auto_evidence"]
    assert by["noev"]["auto_evidence"] == "" and by["noev"]["trigger"] == "wake" and by["noev"]["peak_raw"] == "0.9900"
    assert by["untouched"] == before["untouched"]
    assert n["relabel"] == 1 and n["owner_conflicts"] == 1 and n["conflict_added"] == 1 and n["conflict_dropped"] == 1
    again = [dict(r) for r in rows]
    n2 = A.merge_trigger_only(rows, results)                                               # idempotent
    assert rows == again and n2["relabel"] == 0 and n2["relabel_total"] == 1


def test_trigger_only_on_a_clip_with_a_fake_scorer(tmp_path):
    x = burst(6.0, 1.0, 5.0)
    x[0] = 11
    C.write_wav_new(tmp_path / "wake_tail.wav", x)
    C.write_wav_new(tmp_path / "stt.wav", burst(3.0, 0.2, 2.5, seed=2))
    row = blank("a", model="oye_kivi_next", wake_len="13.12", stt_run="")
    sc = FakeScorer({11: 0.99})
    res = A.trigger_only(row, tmp_path, sc)
    assert sc.n_total == 209920 and res["trigger"] == "wake" and res["cross"] == ("same", "cross")
    assert res["peak_t_s"] == "5.50" and res["peak_raw"] == "0.9900" and res["phrase_end_s"] == "5.50" and res["v5"] == "0.99"
    assert A.trigger_only(row, tmp_path / "nope", sc) is None


def test_cmd_trigger_only_never_loads_the_text_models(tmp_path):
    root = make_dataset(tmp_path)
    table = root / "live" / "live_wakes.csv"
    rows = L.read_table(table)
    for r in rows:
        r["auto_evidence"] = "ph=0;v4=0.0;v5=0.0;own2=0.0;pe_src=;pe_ok=;trig=wake/same/cross;sp3=0.0"
        r["trigger"] = "wake"
    L.write_table(table, rows)
    owner_before = {r["run_id"]: (r["label"], r["speaker"]) for r in rows}
    a = Namespace(dataset_root=str(root), dry_run=False, trigger_only=True, work="/nonexistent", pgrep=lambda: False, wait_kw={})
    out = []
    L.cmd_autolabel(a, out=out.append, scorer=FakeScorer({11: 0.99, 12: 0.0, 13: 0.0, 14: 0.99, 15: 0.0}))
    new = {r["run_id"]: r for r in L.read_table(table)}
    assert [new[f"Kiwi/{m}"]["trigger"] for m in (11, 12, 13, 14, 15)] == ["wake", "button", "button", "wake", "button"]
    assert all(new[k]["label"] == v[0] and new[k]["speaker"] == v[1] for k, v in owner_before.items())
    assert "conflict=owner-label-vs-button" in new["Kiwi/15"]["auto_evidence"]               # owner label true, button now
    assert new["Kiwi/11"]["peak_t_s"] == "5.50" and list(table.parent.glob("live_wakes.csv.bak-*"))
    assert any("trigger after" in o for o in out)
