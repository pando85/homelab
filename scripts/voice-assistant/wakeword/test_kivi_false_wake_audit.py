"""Tests of kivi_false_wake_audit.py. Synthetic tables and synthetic owner labels only: no audio, no real label.

    cd scripts/voice-assistant/wakeword && OMP_NUM_THREADS=4 \
        $KIVI_HOME/.venv/bin/python -m pytest test_kivi_false_wake_audit.py -q
"""
import os
import random
import sys
from argparse import Namespace
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "4")

import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kivi_false_wake_audit as A  # noqa: E402
import kivi_live_wakes as L  # noqa: E402


def utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------------------------------------------------------------------------------------------
# time, model, exposure
# ---------------------------------------------------------------------------------------------


def test_madrid_evening_and_day_role():
    assert A.madrid(utc("2026-11-11T16:59:59Z")) == ("2026-11-11", "test", False)     # 17:59:59 Madrid
    assert A.madrid(utc("2026-11-11T17:00:00Z")) == ("2026-11-11", "test", True)      # 18:00 Madrid
    assert A.madrid(utc("2026-11-11T22:59:59Z")) == ("2026-11-11", "test", True)
    assert A.madrid(utc("2026-11-11T23:00:00Z")) == ("2026-11-12", "mine", False)     # midnight Madrid: next day, even


def test_model_live_v6_window_and_eras():
    assert A.model_live("oye_kivi_next", utc("2026-11-11T22:10:59Z")) == "v5"
    assert A.model_live("oye_kivi_next", utc("2026-11-11T22:11:00Z")) == "v6"
    assert A.model_live("oye_kivi_next", utc("2026-11-11T22:39:59Z")) == "v6"
    assert A.model_live("oye_kivi_next", utc("2026-11-11T22:40:00Z")) == "v5"
    assert A.model_live("oye_kivi", utc("2026-11-08T10:00:00Z")) == "v4"


def test_exposure_nominal_evenings():
    until = "2026-11-11"
    assert A.exposure("c3-b", "v4", until) == pytest.approx(0.5, abs=0.005)   # 20:59:21 to 24:00 on 2026-11-07, about 3 h
    # v5 on c3-a: evenings 08, 09, 10 in full, 11-11 from 18:00 to 23:11 and from 23:40 to 24:00 (v6 in between)
    assert A.exposure("c3-a", "v5", until) == pytest.approx(3 + (5 + 11 / 60 + 20 / 60) / 6)
    assert A.exposure("c3-a", "v6", until) == pytest.approx(29 / 60 / 6)                      # 22:11 to 22:40 is 29 minutes
    # the eras of one TV tile the interval: the evening hours of all models add up to the evenings covered
    total = sum(A.exposure("c3-a", m, until) for m in A.MODELS)
    assert total == pytest.approx(0.5 + 4, abs=0.005)                            # about 3 h on 11-07 plus four full evenings


def test_poisson_ci_and_rate_format():
    lo, hi = A.poisson_ci(0, 1.0)
    assert lo == 0 and hi == pytest.approx(3.689, abs=0.01)
    lo, hi = A.poisson_ci(10, 2.0)
    assert (lo, hi) == pytest.approx((2.398, 9.2), abs=0.05)
    assert A.fmt_rate(3, 0.08).startswith("n/a")


# ---------------------------------------------------------------------------------------------
# synthetic tables
# ---------------------------------------------------------------------------------------------

STRATA_SIZES = {"auto-false-low": {("c3-b", "test"): 10, ("c3-b", "mine"): 3, ("c3-a", "mine"): 2, ("c3-a", "test"): 1},
                "auto-false-high": {("c3-b", "test"): 6, ("c3-b", "mine"): 3, ("c3-a", "mine"): 1},
                "auto-true": {("c3-b", "test"): 13, ("c3-b", "mine"): 2, ("c3-a", "mine"): 4, ("c3-a", "test"): 3},
                "auto-unclear": {("c3-b", "test"): 9, ("c3-b", "mine"): 7, ("c3-a", "mine"): 2, ("c3-a", "test"): 6},
                "auto-not-a-wake-wake": {("c3-b", "mine"): 2},
                "auto-not-a-wake-button": {("c3-b", "mine"): 5, ("c3-b", "test"): 5}}
LABEL_OF = {"auto-false-low": "false", "auto-false-high": "false", "auto-true": "true", "auto-unclear": "unclear", "auto-not-a-wake-wake": "not-a-wake",
            "auto-not-a-wake-button": "not-a-wake"}


def audit_row(i, stratum, dev, role):
    label = LABEL_OF[stratum]
    return {"kind": "live", "table": "live_wakes.csv", "id": f"Kiwi/{i:06d}", "device": dev,
            "madrid_date": "2026-11-11" if role == "test" else "2026-11-10", "day_role": role,
            "evening": "yes" if i % 2 else "no", "model_live": "v5", "trigger": "button" if stratum.endswith("button") else "wake",
            "label": label, "label_source": "auto-v1",
            "label_conf": {"auto-false-low": "0.75", "auto-false-high": "0.95"}.get(stratum, "0.9"), "speaker": "unknown", "cause": "", "near_phrase": 0}


def synthetic_rows():
    rows, i = [], 0
    for st, cells in STRATA_SIZES.items():
        for (dev, role), n in cells.items():
            for _ in range(n):
                i += 1
                rows.append(audit_row(i, st, dev, role))
    # rows the sample never touches: an owner label, a no-source false label, an unknown trigger
    rows.append({**audit_row(900, "auto-false-high", "c3-b", "test"), "label_source": "owner"})
    rows.append({**audit_row(901, "auto-false-high", "c3-b", "test"), "label_source": "no-source"})
    rows.append({**audit_row(902, "auto-false-high", "c3-b", "test"), "trigger": "unknown"})
    return rows


def key_from(rows, ids):
    return [{"sample_id": ids.get(r["id"], ""), "stratum": A.stratum_of(r) or "", "cell": A.cell_of(r), "run_id": r["id"],
             **{k: r[k] for k in ("device", "madrid_date", "day_role", "evening", "model_live", "trigger", "label",
                                  "label_source", "label_conf", "speaker")}} for r in rows]


SIZES = {n: k for n, _l, _t, k in A.STRATA}


def test_default_sample_sizes_are_40_with_5_not_a_wake():
    assert sum(SIZES.values()) == 40 and SIZES["auto-not-a-wake-wake"] + SIZES["auto-not-a-wake-button"] == 5
    assert SIZES["auto-false-low"] + SIZES["auto-false-high"] == 15
    assert [SIZES[s] for s in ("auto-true", "auto-unclear")] == [10, 10]


def test_allocate_equal_share_with_caps():
    rng = random.Random(1)
    got = A.allocate({"a": 3, "b": 1, "c": 6, "d": 16}, 15, rng)
    assert got["a"] == 3 and got["b"] == 1 and sorted((got["c"], got["d"])) == [5, 6]      # small cells whole, rest shared
    assert A.allocate({"a": 2}, 5, rng) == {"a": 2}                                   # never more than the pool


def test_draw_sample_covers_both_devices_and_parities_and_is_deterministic():
    rows = synthetic_rows()
    ids = A.draw_sample(rows, SIZES, seed=7)
    assert ids == A.draw_sample(rows, SIZES, seed=7) and ids != A.draw_sample(rows, SIZES, seed=8)
    by_id = {r["id"]: r for r in rows}
    per = Counter(A.stratum_of(by_id[i]) for i in ids)
    assert dict(per) == SIZES
    assert sorted(ids.values()) == [f"v{i:02d}" for i in range(1, 41)]
    for st in ("auto-true", "auto-unclear"):
        cells = Counter(A.cell_of(by_id[i]) for i in ids if A.stratum_of(by_id[i]) == st)
        assert set(cells) == {"c3-a/mine", "c3-a/test", "c3-b/mine", "c3-b/test"}, st
    both = Counter(by_id[i]["device"] for i in ids if A.stratum_of(by_id[i]).startswith("auto-false"))
    assert set(both) == {"c3-a", "c3-b"}
    assert not {r["id"] for r in rows if r["label_source"] != "auto-v1" or r["trigger"] == "unknown"} & set(ids)
    # the sample id says nothing about the stratum: ids do not sort by stratum
    order = [A.stratum_of(by_id[i]) for i, _ in sorted(ids.items(), key=lambda kv: kv[1])]
    assert order != sorted(order)


def test_optional_nosource_stratum():
    rows = synthetic_rows() + [{**audit_row(950 + k, "auto-false-high", "c3-b", "mine"), "label_source": "no-source"}
                               for k in range(4)]
    ids = A.draw_sample(rows, {**SIZES, A.NOSOURCE_STRATUM: 3}, seed=1)
    by_id = {r["id"]: r for r in rows}
    assert Counter(A.stratum_of(by_id[i]) for i in ids)[A.NOSOURCE_STRATUM] == 3


# ---------------------------------------------------------------------------------------------
# analysis on synthetic owner labels
# ---------------------------------------------------------------------------------------------


def owner_labels(rows, ids, truth_rates, rng):
    """Synthetic owner: for each sampled clip a label drawn from the stratum's confusion row."""
    by_id = {r["id"]: r for r in rows}
    val = []
    for rid, sid in ids.items():
        st = A.stratum_of(by_id[rid])
        probs = truth_rates[st]
        lab = rng.choices(A.CLASSES, weights=[probs[c] for c in A.CLASSES])[0]
        val.append({"sample_id": sid, "run_id": rid, "device": by_id[rid]["device"], "utc": "", "label": lab,
                    "cause": rng.choice(A.CAUSES) if lab == "false" else "",
                    "speaker": "spk01" if lab in ("true", "unclear") else ""})
    return val


PERFECT = {"auto-false-low": {"true": 0, "false": 1, "unclear": 0, "not-a-wake": 0},
           "auto-false-high": {"true": 0, "false": 1, "unclear": 0, "not-a-wake": 0},
           "auto-true": {"true": 1, "false": 0, "unclear": 0, "not-a-wake": 0},
           "auto-unclear": {"true": 0.3, "false": 0.5, "unclear": 0.2, "not-a-wake": 0},
           "auto-not-a-wake-wake": {"true": 0, "false": 0, "unclear": 0, "not-a-wake": 1},
           "auto-not-a-wake-button": {"true": 0, "false": 0, "unclear": 0, "not-a-wake": 1}}


def run_analysis(rows, ids, val, draws=600):
    return A.analyze(key_from(rows, ids), val, "2026-11-11", draws=draws, seed=5)


def test_analyze_perfect_autolabel_confusion_and_precision():
    rows = synthetic_rows()
    ids = A.draw_sample(rows, SIZES, seed=3)
    val = owner_labels(rows, ids, PERFECT, random.Random(0))
    res = run_analysis(rows, ids, val)
    assert res["labelled"] == res["sampled"] == 40
    conf = res["confusion_sample"]
    assert conf["false"]["false"] == 15 and sum(conf["false"].values()) == 15
    assert conf["true"]["true"] == 10 and conf["not-a-wake"]["not-a-wake"] == 5
    assert sum(conf["unclear"].values()) == 10
    mean, lo, hi = res["precision_auto_false"]
    assert 0.9 < mean < 1.0 and lo > 0.75 and hi == pytest.approx(1.0)      # 15 of 15 sampled: the 11 others are drawn
    assert res["p_precision_ge_90"] > 0.5
    assert res["auto_false_owner_true"][0] < 0.05
    # recall: owner-false is also found in auto-unclear (about half of those clips), so auto-false misses part of them
    rec = res["recall_auto_false"]
    assert 0.5 < rec[0] < 0.95 and rec[1] < rec[0] < rec[2]


def test_analyze_recall_high_when_unclear_and_true_hold_no_false():
    rows = synthetic_rows()
    ids = A.draw_sample(rows, SIZES, seed=3)
    rates = {**PERFECT, "auto-unclear": {"true": 0, "false": 0, "unclear": 1, "not-a-wake": 0}}
    res = run_analysis(rows, ids, owner_labels(rows, ids, rates, random.Random(0)))
    # no false clip seen outside auto-false: recall is near 1; the +0.5 prior keeps it below 1 for the clips not sampled
    assert 0.8 < res["recall_auto_false"][0] < 1.0 and res["recall_auto_false"][2] == pytest.approx(1.0)


def test_analyze_census_has_no_uncertainty():
    """Every auto-false clip sampled and labelled: precision is exact, the interval has zero width."""
    rows = [r for r in synthetic_rows() if A.stratum_of(r) in ("auto-false-low", "auto-false-high", "auto-true")]
    ids = {r["id"]: f"v{i:02d}" for i, r in enumerate(rows, 1)}
    val = owner_labels(rows, ids, PERFECT, random.Random(1))
    for v in val[:5]:
        if v["label"] == "false":
            v["label"], v["cause"] = "true", ""                       # the owner finds 5 real attempts among the false
    n_false = sum(1 for v in val if v["run_id"] and v["label"] == "false")
    res = run_analysis(rows, ids, val)
    mean, lo, hi = res["precision_auto_false"]
    assert mean == pytest.approx(n_false / 26) and hi - lo < 1e-9


def test_analyze_confidence_interval_covers_known_truth():
    rows = synthetic_rows()
    one = {"true": 0.1, "false": 0.8, "unclear": 0.1, "not-a-wake": 0.0}
    truth = {"auto-false-low": one, "auto-false-high": one}
    rates = {**PERFECT, **truth}
    pop_false = [r for r in rows if (A.stratum_of(r) or "").startswith("auto-false")]
    covered, n_runs = 0, 40
    for seed in range(n_runs):
        rng = random.Random(100 + seed)
        ids = A.draw_sample(rows, SIZES, seed=seed)
        val = owner_labels(rows, ids, rates, rng)
        # the "true" precision of this synthetic world is the realised owner-false share of the 26 clips: draw the
        # unsampled clips with the same rates to get one world, then compare to the estimate from the sample
        world = [rng.choices(A.CLASSES, weights=[one[c] for c in A.CLASSES])[0] for _ in pop_false]
        sampled = {r["id"]: v["label"] for r in pop_false for v in val if v["run_id"] == r["id"]}
        real = [sampled.get(r["id"], world[j]) for j, r in enumerate(pop_false)]
        true_prec = real.count("false") / len(real)
        res = run_analysis(rows, ids, val, draws=300)
        covered += res["precision_auto_false"][1] <= true_prec <= res["precision_auto_false"][2]
    assert covered / n_runs >= 0.85


def test_analyze_corrected_rate_uses_owner_and_nosource_labels_as_given():
    rows = [r for r in synthetic_rows() if r["evening"] == "yes" or r["label_source"] != "auto-v1"]
    ids = A.draw_sample(rows, {**SIZES}, seed=2)
    val = owner_labels(rows, ids, PERFECT, random.Random(4))
    res = run_analysis(rows, ids, val)
    by = {(r["device"], r["model"]): r for r in res["rates"]}
    b = by[("c3-b", "v5")]
    # the owner-labelled false row (evening) counts once, the no-source false row once if its evening flag is yes
    assert b["wakes"] > 0 and b["corrected_false_evening"][1] <= b["corrected_false_evening"][0] <= b["corrected_false_evening"][2]
    assert by[("c3-b", "all")]["exposure"] == pytest.approx(A.exposure_all("c3-b", "2026-11-11"))
    assert by[("c3-b", "v5")]["corrected_per_evening"] is not None


def test_analyze_rejects_unknown_labels_and_renders():
    rows = synthetic_rows()
    ids = A.draw_sample(rows, SIZES, seed=3)
    val = owner_labels(rows, ids, PERFECT, random.Random(0))
    text = A.render_analysis(run_analysis(rows, ids, val))
    assert "precision of auto-false" in text and "Confusion matrix" in text
    val[0]["label"] = "maybe"
    with pytest.raises(ValueError):
        run_analysis(rows, ids, val)
    val[0]["label"], val[0]["cause"] = "false", "echo"
    with pytest.raises(ValueError):
        run_analysis(rows, ids, val)


def test_analyze_with_no_labels_does_not_crash():
    rows = synthetic_rows()
    ids = A.draw_sample(rows, SIZES, seed=3)
    val = [{"sample_id": s, "run_id": r, "device": "c3-b", "utc": "", "label": "", "cause": "", "speaker": ""}
           for r, s in ids.items()]
    res = run_analysis(rows, ids, val, draws=50)
    assert res["labelled"] == 0
    assert 0.0 <= res["precision_auto_false"][0] <= 1.0


# ---------------------------------------------------------------------------------------------
# end to end on a temporary dataset root
# ---------------------------------------------------------------------------------------------


def write_live(root, rows):
    live = root / "live"
    live.mkdir(parents=True, exist_ok=True)
    out = []
    for r in rows:
        out.append({k: "" for k in L.FIELDS} | {
            "run_id": r["id"], "device": r["device"], "model": "oye_kivi_next",
            "utc": "20261110T180000.000Z" if r["day_role"] == "mine" else "20261111T180000.000Z",
            "wake_len": "5", "stt_len": "3", "label": r["label"], "label_source": r["label_source"].replace("no-source", ""),
            "trigger": r["trigger"], "label_conf": r["label_conf"], "speaker": r["speaker"], "pipeline": "Kiwi"})
    L.write_table(live / "live_wakes.csv", out)


def test_cmd_sample_writes_blind_table_and_frozen_key_and_queue_offers_it_first(tmp_path, monkeypatch):
    monkeypatch.setenv("KIVI_HOME", str(tmp_path / "kh"))
    (tmp_path / "kh").mkdir()
    root = tmp_path / "real"
    rows = synthetic_rows()
    write_live(root, rows)
    ns = Namespace(dataset_root=str(root), until=None, seed=11, n_nosource=0)
    out = []
    val = A.cmd_sample(ns, out=out.append)
    assert len(val) == 40
    live = root / "live"
    assert oct((live / L.VALIDATION_TABLE).stat().st_mode & 0o777) == "0o600"
    head = (live / L.VALIDATION_TABLE).read_text().splitlines()[0]
    assert head == ",".join(L.VALIDATION_FIELDS) and "stratum" not in head and "auto" not in head
    keyrows = A.read_csv_rows(live / L.VALIDATION_KEY, A.KEY_FIELDS)
    assert len(keyrows) == len(rows) and sum(1 for k in keyrows if k["sample_id"]) == 40
    assert (live / "autolabel_validation_key.json").exists()
    assert any("owner command" in x and "kivi_live_wakes.py queue" in x for x in out)
    # not overwritten
    with pytest.raises(SystemExit):
        A.cmd_sample(ns, out=lambda *_: None)
    # the queue offers the 40 clips first
    items = L.queue_items(root)
    assert [it["group"] for it in items[:40]] == [0] * 40 and all(it["group"] != 0 for it in items[40:])
    # owner labels two clips; the key still holds the auto labels, the live table the owner's
    first = items[0]["row"]
    L.save_validation_label(root, first["sample_id"], "true", "", "spk02")
    key_after = {k["run_id"]: k for k in A.read_csv_rows(live / L.VALIDATION_KEY, A.KEY_FIELDS)}
    live_after = {r["run_id"]: r for r in L.read_table(live / "live_wakes.csv")}
    assert key_after[first["run_id"]]["label_source"] == "auto-v1"
    assert live_after[first["run_id"]]["label_source"] == "owner" and live_after[first["run_id"]]["label"] == "true"
    res = A.cmd_analyze(Namespace(dataset_root=str(root), key=None, validation=None, draws=60, seed=1), out=lambda *_: None)
    assert res["labelled"] == 1 and res["sampled"] == 40


def test_cmd_table_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("KIVI_HOME", str(tmp_path / "kh"))
    (tmp_path / "kh").mkdir()
    root = tmp_path / "real"
    write_live(root, synthetic_rows())
    (root / "live-misses").mkdir()
    L.write_miss_table(root / "live-misses" / "c3_events.csv", [
        {k: "" for k in L.MISS_FIELDS} | {"id": "e1", "device": "c3-b", "utc": "20261111T180000.000Z", "label": "other-speech",
                                         "speaker": "spk02", "clip": "x"},
        {k: "" for k in L.MISS_FIELDS} | {"id": "e2", "device": "c3-a", "utc": "20261109T100000.000Z", "clip": "y"}])
    out = []
    audit = A.cmd_table(Namespace(dataset_root=str(root), until=None, out=str(tmp_path / "a" / "t.csv")), out=out.append)
    assert len(audit) == len(synthetic_rows()) + 2
    text = "\n".join(out)
    assert "Live false wakes per TV and model" in text and "{'spk02': 1}" in text
    replay = [r for r in audit if r["kind"] == "replay"]
    assert (replay[0]["evening"], replay[0]["label_source"]) == ("yes", "owner")
    assert (replay[1]["evening"], replay[1]["label_source"], replay[1]["madrid_date"]) == ("no", "", "2026-11-09")
