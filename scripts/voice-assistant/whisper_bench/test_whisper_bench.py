"""Self-tests of the whisper_bench parsing and metrics code (no server, no GPU, no audio).

Run: python3 -m pytest scripts/voice-assistant/whisper_bench   (needs numpy and pytest)
"""
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import analyze  # noqa: E402
import metrics as m  # noqa: E402
import select_subset  # noqa: E402


def test_normalize_numbers_and_symbols():
    assert m.normalize("Pon la luz al veinte por ciento") == m.normalize("pon la luz al 20 %")
    assert m.normalize("Sube a 21,5 grados") == m.normalize("sube a veintiuno y medio grados")
    assert m.normalize("Sube a 21°") == m.normalize("sube a veintiuno grados")
    assert m.normalize("¡Enciende la LUZ, del Salón!") == ["enciende", "la", "luz", "del", "salon"]
    assert m.normalize("") == [] and m.normalize(None) == []


def test_article_un_is_not_a_number():
    assert m.normalize("enciende un ventilador") == ["enciende", "un", "ventilador"]


def test_edit_distance_and_errors():
    assert m.edit_distance("kitten", "sitting") == 3
    assert m.edit_distance([], ["a", "b"]) == 2
    assert m.word_errors("apaga la luz del salón", "apaga la luz de salón") == (1, 5)
    assert m.word_errors("pon veinte", "pon 20") == (0, 2)
    e, n = m.char_errors("apaga la luz", "apaga la luces")
    assert (e, n) == (3, 12)


def test_exact_match():
    assert m.exact_match("Apaga la luz.", "apaga la luz")
    assert not m.exact_match("Apaga la luz", "apaga las luces")


def test_percentile():
    assert m.percentile([1, 2, 3, 4, 5], 50) == 3
    assert m.percentile([1, 2, 3, 4], 50) == 2.5
    assert m.percentile([10], 90) == 10
    assert abs(m.percentile(list(range(101)), 90) - 90) < 1e-9


def test_mcnemar_exact():
    assert m.mcnemar_exact([True, False], [True, False]) == (0, 0, 1.0)
    fixed, broken, p = m.mcnemar_exact([False] * 10, [True] * 10)
    assert (fixed, broken) == (10, 0) and abs(p - 2 / 2 ** 10) < 1e-12
    f, b, p = m.mcnemar_exact([True, True, False, False], [False, True, True, True])
    assert (f, b) == (2, 1) and p == 1.0


def test_paired_bootstrap_detects_shift():
    base = [0.9 + 0.01 * (i % 7) for i in range(100)]
    other = [x + 0.3 for x in base]
    d, lo, hi = m.paired_bootstrap(base, other, "median", n_boot=500)
    assert abs(d - 0.3) < 1e-9 and lo > 0.2 and hi < 0.4
    d, lo, hi = m.paired_bootstrap(base, base, "mean", n_boot=200)
    assert d == 0 and lo == 0 and hi == 0


def test_wilson():
    lo, hi = m.wilson(50, 100)
    assert 0.4 < lo < 0.5 < hi < 0.6


def test_stratified_pick_is_deterministic_and_balanced():
    items = [{"id": f"c{i}", "kind": "command", "style": "s%d" % (i % 4), "audio": True} for i in range(80)]
    items += [{"id": f"q{i}", "kind": "question", "style": "general", "audio": True} for i in range(20)]
    a = select_subset.pick(items, per_style=3, per_other={"question": 2}, seed=7)
    b = select_subset.pick(items, per_style=3, per_other={"question": 2}, seed=7)
    assert [i["id"] for i in a] == [i["id"] for i in b]
    assert sum(i["kind"] == "command" for i in a) == 12 and sum(i["kind"] == "question" for i in a) == 2
    assert select_subset.pick(items, 3, {"question": 2}, seed=8) != a


def test_parse_results_and_summary(tmp_path):
    p = tmp_path / "results.jsonl"
    rows = [
        {"config": "a", "id": "x", "cond": "clean", "latency_s": 1.0, "text": "apaga la luz"},
        {"config": "a", "id": "y", "cond": "clean", "latency_s": 2.0, "text": "pon veinte"},
        {"config": "bad line without json"},
    ]
    with open(p, "w") as f:
        for r in rows[:2]:
            f.write(json.dumps(r) + "\n")
        f.write("not json\n\n")
    got = analyze.load_results(p)
    assert set(got) == {"a"} and len(got["a"]) == 2
    items = {"x": {"kind": "command", "text": "Apaga la luz."}, "y": {"kind": "command", "text": "Pon 20"}}
    s = analyze.summarize(got["a"], items)
    assert s["clean"]["n"] == 2 and s["clean"]["exact"] == (2, 2)
    assert s["clean"]["lat_p50"] == 1.5 and s["clean"]["wer"] == 0.0


def test_load_manifest(tmp_path):
    p = tmp_path / "manifest.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "cond", "path", "audio_s", "kind", "style"])
        w.writerow(["a-1", "clean", "clean/a-1.wav", "3.0", "command", "direct"])
    rows = analyze.load_manifest(p)
    assert rows[0]["audio_s"] == 3.0


def test_live_overlap_parse_and_filter():
    import live_overlap as lo
    lines = [
        "2026-11-05T20:00:10.500000000+01:00 DEBUG:x.dispatch_handler:Completed request in 2.0 second(s)",
        "2026-11-05T20:00:10.5Z DEBUG:other line without the marker",
        "garbage",
    ]
    iv = lo.parse_intervals(lines)
    assert len(iv) == 1 and abs((iv[0][1] - iv[0][0]) - 2.0) < 1e-6
    end = iv[0][1]
    rows = [{"id": "a", "cond": "clean", "t": end + 0.5, "latency_s": 0.4},   # overlaps
            {"id": "b", "cond": "clean", "t": end + 30, "latency_s": 0.9}]   # far away
    assert lo.overlapped(rows, iv, margin_s=1.0) == {("a", "clean")}
