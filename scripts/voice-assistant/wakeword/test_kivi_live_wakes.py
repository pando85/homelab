"""Tests of kivi_live_wakes.py. Synthetic signals only, a fake fetcher and a fake player: nothing is played.

    cd scripts/voice-assistant/wakeword && OMP_NUM_THREADS=4 \
        $KIVI_HOME/.venv/bin/python -m pytest test_kivi_live_wakes.py -q
"""
import os
import stat
import sys
from argparse import Namespace
from pathlib import Path

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "4")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kivi_extract as X  # noqa: E402
import kivi_live_wakes as L  # noqa: E402
import kivi_real_common as C  # noqa: E402

RATE = C.RATE
BOOT = L.epoch(L.BOOT_UTC)
T0 = L.epoch("2026-11-09T10:00:00Z")      # in the oye_kivi_next era on both devices
NOW = T0 + 100_000


@pytest.fixture(autouse=True)
def no_trigger_scorer(monkeypatch):
    """extract replays the wake models on every tail: not in these tests (see test_extract_trigger_column)."""
    monkeypatch.setattr(L, "make_trigger_scorer", lambda a: None)


def ramp(sec, off=0):
    return ((np.arange(int(sec * RATE)) + off) % 30000).astype(np.int16)


def put(path, x, end):
    path.parent.mkdir(parents=True, exist_ok=True)
    hdr = C.wav_header(0)
    path.write_bytes(hdr[:4] + b"\xff\xff\xff\xff" + hdr[8:40] + b"\xff\xff\xff\xff" + x.astype("<i2").tobytes())
    os.utime(path, (end, end))


def run_dir(root, prefix, pipeline, start):
    return root / (prefix + "0000aaaa") / pipeline / str(int(round((start - BOOT) * 1e9)))


class CountingFetcher(X.LocalFetcher):
    def __init__(self, root):
        super().__init__(root)
        self.ranges = []

    def fetch_range(self, path, lo, hi):
        self.ranges.append((path, lo, hi))
        return super().fetch_range(path, lo, hi)


def make_tree(tmp_path):
    rec = tmp_path / "rec"
    wake = lambda d, x, start: put(d / "00_wake-wake_word.openwakeword.wav", x, start + len(x) / RATE)   # noqa: E731
    stt = lambda d, x, start: put(d / "01_stt-stt.faster_whisper.wav", x, start + len(x) / RATE)         # noqa: E731
    # c3-a run 1: 20 s wake file, 3 s stt in the same directory
    s = T0
    d = run_dir(rec, "bd8256", "Kiwi", s)
    wake(d, ramp(20), s)
    stt(d, ramp(3, 7), s + 20)
    # c3-a run 2: closed wake file, no stt: only counted
    s = T0 + 1000
    wake(run_dir(rec, "bd8256", "Kiwi", s), ramp(30), s)
    # c3-b run 3: 4 s wake file (shorter than the tail), the stt run is its own directory 1.5 s later, 1.0 s long
    s = T0 + 2000
    wake(run_dir(rec, "fda18f", "Kiwi", s), ramp(4), s)
    stt(run_dir(rec, "fda18f", "Kiwi", s + 5.5), ramp(1.0), s + 5.5)
    # c3-b run 4: stt hit the cap
    s = T0 + 3000
    d = run_dir(rec, "fda18f", "Kiwi", s)
    wake(d, ramp(10), s)
    stt(d, ramp(15.01), s + 10)
    # c3-b stt-only run far from any wake file: continued conversation
    stt(run_dir(rec, "fda18f", "Kiwi", T0 + 3100), ramp(2), T0 + 3100)
    # c3-a run 5: inside a recorded session window
    s = T0 + 4000
    d = run_dir(rec, "bd8256", "Kiwi", s)
    wake(d, ramp(20), s)
    stt(d, ramp(3), s + 20)
    # c3-a run 6: before the scope start
    s = L.epoch("2026-11-05T10:00:00Z")
    d = run_dir(rec, "bd8256", "Kiwi", s)
    wake(d, ramp(20), s)
    stt(d, ramp(3), s + 20)
    # c3-a run 7: still open (written 10 s before now)
    s = NOW - 40
    d = run_dir(rec, "bd8256", "Kiwi", s)
    wake(d, ramp(20), s)
    stt(d, ramp(3), s + 20)
    return rec


def make_ns(tmp_path, rec):
    ds = tmp_path / "ds"
    (ds / "sess").mkdir(parents=True)
    (ds / "sess" / "session.json").write_text(__import__("json").dumps({
        "device": "c3-a", "t0_utc": X.utc_str(T0 + 3990), "end_utc": X.utc_str(T0 + 4010)}))
    return Namespace(dataset_root=str(ds), devices=None, devices_file=None, from_dir=str(rec), scope_start=L.SCOPE_START,
                     now=NOW, context="x", namespace="x", pod="x", container=None, recordings_root="/x", player=None,
                     again=False, apply=False, test_share=0.30, rule="recent-days", pgrep=lambda: False)


def test_list_buckets_pairing_and_counts(tmp_path):
    rec = make_tree(tmp_path)
    ns = make_ns(tmp_path, rec)
    lines = []
    res = L.cmd_list(ns, X.LocalFetcher(rec), out=lines.append)
    runs = res["runs"]
    assert [(r["device"], r["model"]) for r in runs] == [("c3-a", "oye_kivi_next"), ("c3-b", "oye_kivi_next"), ("c3-b", "oye_kivi_next")]
    rows = [L.run_row(r) for r in runs]
    assert [r["bucket"] for r in rows] == ["suspect-true", "suspect-false", "suspect-false"]
    assert rows[1]["stt_run"] and not rows[0]["stt_run"]                # run 3: the stt run is its own directory
    assert rows[1]["wake_len"] == "4.00" and rows[0]["stt_len"] == "3.00"
    assert res["no_detection"] == {("c3-a", "oye_kivi_next"): 1}
    assert res["in_session"] == 1 and res["open"] == 1 and res["stt_only"] == 1
    assert not res["warnings"]
    assert rows[0]["utc"] == L.dir_stamp(T0)
    assert any("total: 3" in ln for ln in lines)


def test_eras():
    t = L.epoch
    assert L.model_of("c3-b", t("2026-11-07T20:00:00Z")) == "oye_kivi"
    assert L.model_of("c3-a", t("2026-11-08T12:00:00Z")) == "oye_kivi_next"       # the c3-a trial
    assert L.model_of("c3-b", t("2026-11-08T12:00:00Z")) == "oye_kivi"
    assert L.model_of("c3-b", t("2026-11-08T13:30:00Z")) == "oye_kivi_next"
    assert L.model_of("c3-b", t("2026-11-07T19:00:00Z")) is None


def test_extract_clips_table_idempotent_and_labels_kept(tmp_path):
    rec = make_tree(tmp_path)
    ns = make_ns(tmp_path, rec)
    f = CountingFetcher(rec)
    L.cmd_extract(ns, f, out=lambda *_: None)
    live = Path(ns.dataset_root) / "live"
    rows = L.read_table(live / "live_wakes.csv")
    assert len(rows) == 3
    r1 = rows[0]
    d1 = live / "c3-a" / r1["utc"]
    tail = C.read_pcm16(d1 / "wake_tail.wav")
    assert len(tail) == 6 * RATE and (tail == ramp(20)[-6 * RATE:]).all()         # the last 6 s, sample exact
    assert (C.read_pcm16(d1 / "stt.wav") == ramp(3, 7)).all()
    short = [r for r in rows if r["device"] == "c3-b" and r["bucket"] == "suspect-false"]
    assert len(C.read_pcm16(live / "c3-b" / short[0]["utc"] / "wake_tail.wav")) == int(4 * RATE)   # shorter than the tail: whole file
    assert r1["sha256"] == L.sha256_file(d1 / "wake_tail.wav") and len(r1["sha256_stt"]) == 64
    assert (r1["label"], r1["speaker"], r1["split"]) == ("", "", "")
    assert stat.S_IMODE((live / "c3-a").stat().st_mode) == 0o700
    assert stat.S_IMODE((d1 / "stt.wav").stat().st_mode) == 0o600
    assert stat.S_IMODE((live / "live_wakes.csv").stat().st_mode) == 0o600
    # the owner labels one row; a re-run changes nothing and reads nothing again
    rows[0]["label"], rows[0]["speaker"], rows[0]["split"] = "true", "spk01", "train"
    L.write_table(live / "live_wakes.csv", rows)
    f2 = CountingFetcher(rec)
    L.cmd_extract(ns, f2, out=lambda *_: None)
    assert f2.ranges == []
    again = L.read_table(live / "live_wakes.csv")
    assert again == rows and again[0]["label"] == "true"
    # a deleted clip is fetched again, the label stays
    (d1 / "stt.wav").unlink()
    f3 = CountingFetcher(rec)
    L.cmd_extract(ns, f3, out=lambda *_: None)
    assert len(f3.ranges) == 1 and (d1 / "stt.wav").exists()
    assert L.read_table(live / "live_wakes.csv")[0]["label"] == "true"
    assert not list(live.rglob("*.part"))


def test_short_copy_fails_without_final_file(tmp_path):
    rec = make_tree(tmp_path)
    ns = make_ns(tmp_path, rec)

    class Short(X.LocalFetcher):
        def fetch_range(self, path, lo, hi):
            yield from super().fetch_range(path, lo, hi - 2)

    with pytest.raises(SystemExit):
        L.cmd_extract(ns, Short(rec), out=lambda *_: None)
    live = Path(ns.dataset_root) / "live"
    assert not list(live.rglob("*.wav"))


def labelled_table(tmp_path, n=3):
    rec = make_tree(tmp_path)
    ns = make_ns(tmp_path, rec)
    L.cmd_extract(ns, X.LocalFetcher(rec), out=lambda *_: None)
    return ns, Path(ns.dataset_root) / "live" / "live_wakes.csv"


def test_review_order_replay_skip_quit_resume(tmp_path):
    ns, table = labelled_table(tmp_path)
    played = []
    answers = iter(["r", "t", "1",            # first (a suspect-false one): replay, then true + spk01
                    "s",                      # second: skip
                    "f", "4",                 # the third (suspect-true after the two suspect-false ones): false, unknown
                    ])
    out = []
    n = L.cmd_review(ns, player=lambda p: played.append(Path(p)), input_fn=lambda _p: next(answers), out=out.append)
    assert n == 2
    assert [p.name for p in played[:4]] == ["wake_tail.wav", "stt.wav", "wake_tail.wav", "stt.wav"]     # replay played both again
    rows = {r["run_id"]: r for r in L.read_table(table)}
    first = [r for r in L.read_table(table) if r["label"] == "true"]
    assert len(first) == 1 and first[0]["bucket"] == "suspect-false" and first[0]["speaker"] == "spk01"
    skipped = [r for r in rows.values() if not r["label"]]
    assert len(skipped) == 1                                                   # the skipped one is still open
    # resume: only the open one is offered, then quit
    played.clear()
    L.cmd_review(ns, player=lambda p: played.append(Path(p)), input_fn=lambda _p: "q", out=lambda *_: None)
    assert len(played) == 2 and played[0].parent.name == skipped[0]["utc"]
    # invalid keys are asked again
    answers = iter(["x", "u", "9", "3"])
    L.cmd_review(ns, player=lambda p: None, input_fn=lambda _p: next(answers), out=lambda *_: None)
    done = {r["run_id"]: r for r in L.read_table(table)}[skipped[0]["run_id"]]
    assert (done["label"], done["speaker"]) == ("unclear", "spk-other")


def test_review_ctrl_c_saves(tmp_path):
    ns, table = labelled_table(tmp_path)
    answers = iter(["t", "2"])

    def inp(_p):
        try:
            return next(answers)
        except StopIteration:
            raise KeyboardInterrupt

    assert L.cmd_review(ns, player=lambda p: None, input_fn=inp, out=lambda *_: None) == 1
    assert sum(1 for r in L.read_table(table) if r["label"] == "true") == 1


def row(run, utc, label, speaker):
    return {k: "" for k in L.FIELDS} | {"run_id": run, "utc": utc, "label": label, "speaker": speaker, "device": "c3-a"}


def test_split_by_madrid_day_per_speaker():
    rows = []
    # spk01: 10 true wakes on 4 local days; a wake at 23:30Z on 11-09 is already 11-10 in Madrid (CET)
    for i, utc in enumerate(["20261108T100000.000Z"] * 3 + ["20261109T100000.000Z"] * 3 + ["20261109T233000.000Z"] * 2
                            + ["20261110T120000.000Z"] * 2):
        rows.append(row(f"a{i}", utc, "true", "spk01"))
    # spk02: all on one day, stays unassigned
    for i in range(3):
        rows.append(row(f"b{i}", "20261109T100000.000Z", "true", "spk02"))
    rows.append(row("f1", "20261108T100000.000Z", "false", "unknown"))
    rows.append(row("f2", "20261110T150000.000Z", "false", "unknown"))
    rows.append(row("u1", "20261110T150000.000Z", "unclear", "unknown"))
    assert L.local_day("20261109T233000.000Z") == "2026-11-10"
    assign, info = L.propose_split(rows)
    # the most recent local day (11-10: 4 wakes) already holds 40 % >= 30 %: test; the other days train
    assert info["spk01"]["test_days"] == ["2026-11-10"] and info["spk01"]["test"] == 4 and info["spk01"]["train"] == 6
    assert {assign[f"a{i}"] for i in (0, 1, 2, 3, 4, 5)} == {"train"} and {assign["a6"], assign["a7"], assign["a8"], assign["a9"]} == {"test"}
    assert "b0" not in assign and "left unassigned" in info["spk02"]["note"]
    assert assign["f1"] == "train" and assign["f2"] == "test" and "u1" not in assign


def test_split_command_apply(tmp_path):
    ns, table = labelled_table(tmp_path)
    rows = L.read_table(table)
    assert L.cmd_split(ns, out=lambda *_: None) == {}                       # nothing labelled
    for i, r in enumerate(rows):
        r["label"], r["speaker"] = "true", "spk01"
        r["utc"] = ["20261108T100000.000Z", "20261109T100000.000Z", "20261110T100000.000Z"][i]
    L.write_table(table, rows)
    out = []
    ns.apply = False
    L.cmd_split(ns, out=out.append)
    assert all(r["split"] == "" for r in L.read_table(table)) and any("proposal only" in o for o in out)
    ns.apply = True
    L.cmd_split(ns, out=out.append)
    assert [r["split"] for r in L.read_table(table)] == ["train", "train", "test"]


def test_extract_trigger_column(tmp_path):
    """The trigger of a new row comes from the live model's score on the tail and from where its request audio sits."""
    rec = make_tree(tmp_path)
    ns = make_ns(tmp_path, rec)

    class Scorer:
        def peaks(self, pcm, n_total=None):
            hi = int(pcm[-1]) % 2 == 0
            v = 0.99 if hi else 0.0
            return {"oye_kivi_next": (v, v, v, 5.5), "oye_kivi": (v, v, v, 5.5)}

    L.cmd_extract(ns, X.LocalFetcher(rec), out=lambda *_: None, scorer=Scorer())
    live = Path(ns.dataset_root) / "live"
    rows = L.read_table(live / "live_wakes.csv")
    assert {r["trigger"] for r in rows} <= {"wake", "button"} and all(r["trigger"] for r in rows)
    sep = [r for r in rows if r["stt_run"]]
    assert len(sep) == 1
    # a re-run with a different scorer never changes an existing trigger
    before = [r["trigger"] for r in rows]
    L.cmd_extract(ns, X.LocalFetcher(rec), out=lambda *_: None, scorer=Scorer())
    assert [r["trigger"] for r in L.read_table(live / "live_wakes.csv")] == before


def test_list_reports_separate_stt_runs(tmp_path):
    rec = make_tree(tmp_path)
    out = []
    L.cmd_list(make_ns(tmp_path, rec), X.LocalFetcher(rec), out=out.append)
    assert any("separate stt-only run: 1" in o for o in out)


def test_review_only_uncertain_and_owner_source(tmp_path):
    ns, table = labelled_table(tmp_path)
    rows = L.read_table(table)
    rows[0].update(label="true", label_source="auto-v1", label_conf="0.98", speaker="spk01", speaker_conf="0.9")
    rows[1].update(label="unclear", label_source="auto-v1", label_conf="0.50", speaker="unknown")
    rows[2].update(label="true", label_source="owner", label_conf="1.00", speaker="spk01")
    L.write_table(table, rows)
    todo = L.review_order(L.read_table(table), only_uncertain=True)
    assert [r["run_id"] for r in todo] == [rows[1]["run_id"]]
    ns.only_uncertain = True
    L.cmd_review(ns, player=lambda p: None, input_fn=lambda _p, it=iter(["n", "4"]): next(it), out=lambda *_: None)
    got = {r["run_id"]: r for r in L.read_table(table)}
    assert got[rows[1]["run_id"]]["label"] == "not-a-wake" and got[rows[1]["run_id"]]["label_source"] == "owner"
    assert got[rows[0]["run_id"]]["label_source"] == "auto-v1"                        # confident auto label untouched


def test_review_saves_into_a_fresh_read(tmp_path):
    ns, table = labelled_table(tmp_path)

    def player(_p):
        rows = L.read_table(table)                      # another tool writes a label while the owner listens
        for r in rows:
            if not r["label"]:
                r["label"], r["label_source"] = "false", "auto-v1"
                break
        L.write_table(table, rows)

    L.cmd_review(ns, player=player, input_fn=lambda _p, it=iter(["t", "1", "q"]): next(it), out=lambda *_: None)
    rows = L.read_table(table)
    assert sum(1 for r in rows if r["label_source"] == "auto-v1") >= 1                # the other tool's write survived
    assert sum(1 for r in rows if r["label_source"] == "owner") == 1


def test_split_alternate_day_rule(tmp_path):
    ns, table = labelled_table(tmp_path)
    rows = L.read_table(table)
    utcs = ["20261108T100000.000Z", "20261109T100000.000Z", "20261109T233000.000Z"]   # 11-08, 11-09, 11-10 (Madrid)
    for r, u, lab in zip(rows, utcs, ("true", "false", "not-a-wake")):
        r["utc"], r["label"], r["speaker"] = u, lab, "spk01"
    rows[2]["label"] = "true"
    L.write_table(table, rows)
    assign = L.propose_split_alternate(L.read_table(table))
    assert [assign[r["run_id"]] for r in rows] == ["train", "test", "train"]
    assert L.propose_split_alternate([dict(rows[0], label="unclear"), dict(rows[1], label="not-a-wake")]) == {}
    ns.rule, ns.apply = "alternate-day", True
    L.cmd_split(ns, out=lambda *_: None)
    assert [r["split"] for r in L.read_table(table)] == ["train", "test", "train"]


def test_review_miss_candidates_table(tmp_path):
    ds = tmp_path / "ds"
    misses = ds / "live-misses"
    rows = []
    for i, (rank, part) in enumerate([(0.9, "mine-v5era"), (1.4, "test2-v5era"), (1.1, "mine-v5era"), (0.5, "mine-v5era")], 1):
        d = misses / part / "c3-a" / f"2026111{i}T100000.000Z"
        d.mkdir(parents=True)
        (d / "window.wav").write_bytes(C.wav_header(0))
        rows.append({k: "" for k in L.MISS_FIELDS} | {"id": f"mc{i:03d}", "device": "c3-a", "part": part, "utc": d.name,
                                                      "rank": str(rank), "clip": f"{part}/c3-a/{d.name}/window.wav"})
    table = misses / "miss_candidates.csv"
    L.write_miss_table(table, rows)
    assert stat.S_IMODE(table.stat().st_mode) == 0o600
    ns = Namespace(dataset_root=str(ds), table="miss_candidates.csv", player=None, again=False)       # bare name: found under live-misses/
    played, out = [], []
    answers = iter(["x", "a", "5", "2",       # best rank (mc002): invalid keys asked again, attempt, speaker 2
                    "r", "o", "3",            # mc003: replay, other-speech, spk-other
                    "s",                      # mc001: skip
                    "t"])                     # mc004: tv-media, no speaker asked
    n = L.cmd_review(ns, player=lambda p: played.append(Path(p)), input_fn=lambda _p: next(answers), out=out.append)
    assert n == 3 and [p.parent.parent.parent.name for p in played[:1]] == ["test2-v5era"]
    assert [p.name for p in played] == ["window.wav"] * 5                                              # one file per clip, plus the replay
    got = {r["id"]: (r["label"], r["speaker"]) for r in L.read_miss_table(table)}
    assert got == {"mc002": ("attempt", "spk02"), "mc003": ("other-speech", "spk-other"), "mc001": ("", ""), "mc004": ("tv-media", "")}
    assert not any("0.9" in o or "rank" in o for o in out)                                           # scores are not shown
    # a labelled window is never offered again; --again goes through them all; quit saves what was done
    played.clear()
    L.cmd_review(ns, player=lambda p: played.append(Path(p)), input_fn=lambda _p: "q", out=lambda *_: None)
    assert len(played) == 1 and played[0].parent.name == "20261111T100000.000Z"
    ns.again = True
    played.clear()
    L.cmd_review(ns, player=lambda p: played.append(Path(p)), input_fn=lambda _p: "q", out=lambda *_: None)
    assert played[0].parent.name == "20261112T100000.000Z"                                            # best rank first (mc002)
    # the live-wake review is unchanged without --table
    assert getattr(Namespace(), "table", None) is None


# ---------------------------------------------------------------------------------------------
# labelling queue
# ---------------------------------------------------------------------------------------------


def window_row(i, rank, part="d-20261109", device="c3-a", utc=None, label=""):
    utc = utc or f"20261109T1{i % 10}0000.000Z"
    return {k: "" for k in L.MISS_FIELDS} | {"id": f"x{i:03d}", "device": device, "part": part, "utc": utc, "rank": str(rank),
                                             "peak_v5": "0.97", "label": label, "clip": f"{part}/{device}/{utc}/window.wav"}


def queue_setup(tmp_path):
    ns, table = labelled_table(tmp_path)
    root = Path(ns.dataset_root)
    mis = root / "live-misses"
    mis.mkdir(exist_ok=True)
    L.write_miss_table(mis / "miss_candidates.csv", [window_row(1, 0.5), window_row(2, 1.5), window_row(3, 0.9, label="noise")])
    L.write_miss_table(mis / "daily_false_wakes.csv", [window_row(4, 0.1, utc="20261109T130000.000Z"),
                                                       window_row(5, 0.9, utc="20261109T120000.000Z")])
    L.write_miss_table(mis / "c3_events.csv", [window_row(6, 0.2)])
    ns.minutes, ns.max_items, ns.day, ns.device = None, None, None, None
    return ns, table, mis


def test_queue_order_false_wakes_then_uncertain_live_then_misses(tmp_path):
    ns, table, mis = queue_setup(tmp_path)
    items = L.queue_items(ns.dataset_root)
    kinds = [(it["group"], it["table"].name) for it in items]
    assert [g for g, _ in kinds] == sorted(g for g, _ in kinds)                    # groups never interleave
    assert kinds[0] == (0, "daily_false_wakes.csv") and kinds[1] == (0, "daily_false_wakes.csv")
    assert [it["row"]["id"] for it in items[:2]] == ["x005", "x004"]               # false wakes by time, not by score
    live = [it for it in items if it["kind"] == "live"]
    assert len(live) == 3 and all(it["group"] == 1 for it in live)
    misses = [it for it in items if it["group"] == 2]
    assert [it["row"]["id"] for it in misses if it["table"].name == "miss_candidates.csv"] == ["x002", "x001"]   # rank order, labelled gone
    assert items[-1]["table"].name in ("miss_candidates.csv", "c3_events.csv")
    assert L.queue_counts(items) == {(0, "daily_false_wakes.csv"): 2, (1, "live_wakes.csv"): 3,
                                     (2, "c3_events.csv"): 1, (2, "miss_candidates.csv"): 2}


def test_queue_walk_labels_saves_each_and_resumes(tmp_path):
    ns, table, mis = queue_setup(tmp_path)
    played, out = [], []
    # item 1 (false-wake window): attempt, spk02; item 2: skip; then quit
    answers = iter(["a", "2", "s", "q"])
    n = L.cmd_queue(ns, player=lambda p: played.append(Path(p)), input_fn=lambda _p: next(answers), out=out.append)
    assert n == 1
    assert out[0].startswith("pending at start: 8 item(s)") and any("daily_false_wakes.csv: 2" in x for x in out)
    assert "labelled 1 item(s) in this session" in out and "pending at end: 7 item(s)" in out
    assert out[out.index("pending at end: 7 item(s)") + 1] == "  1. replay false wakes of the live model: daily_false_wakes.csv: 1"
    first = {r["id"]: r for r in L.read_miss_table(mis / "daily_false_wakes.csv")}
    assert (first["x005"]["label"], first["x005"]["speaker"]) == ("attempt", "spk02") and first["x004"]["label"] == ""
    assert played[0].name == "window.wav"
    # resume: the skipped x004 is offered first again; label it, then a live clip with true wake keys, then quit
    played.clear()
    answers = iter(["t", "t", "1", "q"])               # window: tv-media (no speaker asked); live clip: true + spk01
    n = L.cmd_queue(ns, player=lambda p: played.append(Path(p)), input_fn=lambda _p: next(answers), out=lambda *_: None)
    assert n == 2
    assert {r["id"]: r["label"] for r in L.read_miss_table(mis / "daily_false_wakes.csv")} == {"x004": "tv-media", "x005": "attempt"}
    assert [p.name for p in played[1:3]] == ["wake_tail.wav", "stt.wav"]
    owner = [r for r in L.read_table(table) if r["label_source"] == "owner"]
    assert len(owner) == 1 and owner[0]["label"] == "true" and owner[0]["speaker"] == "spk01"


def test_queue_limits_empty_and_ctrl_c(tmp_path):
    ns, table, mis = queue_setup(tmp_path)
    ns.max_items = 1
    out = []
    assert L.cmd_queue(ns, player=lambda p: None, input_fn=lambda _p, it=iter(["n", "n"]): next(it), out=out.append) == 1
    assert "session limit reached" in out
    clock = iter([0.0, 0.0, 10_000.0])                          # second item: the 2-minute budget is spent
    ns.max_items, ns.minutes = None, 2
    out = []
    assert L.cmd_queue(ns, player=lambda p: None, input_fn=lambda _p, it=iter(["n", "n"]): next(it), out=out.append,
                       clock=lambda: next(clock)) == 1
    assert "session limit reached" in out
    ns.minutes = None

    def inp(_p, it=iter(["u", "4"])):
        try:
            return next(it)
        except StopIteration:
            raise KeyboardInterrupt
    assert L.cmd_queue(ns, player=lambda p: None, input_fn=inp, out=lambda *_: None) == 1       # Ctrl-C keeps the label
    ns.day = "2020-01-01"
    out = []
    assert L.cmd_queue(ns, player=lambda p: None, input_fn=lambda _p: "q", out=out.append) == 0
    assert out == ["pending at start: 0 item(s) (queue empty)"]


def test_queue_save_is_a_fresh_read_of_the_window_table(tmp_path):
    ns, table, mis = queue_setup(tmp_path)

    def player(_p):                                              # a collector appends a row while the owner listens
        rows = L.read_miss_table(mis / "daily_false_wakes.csv")
        if len(rows) == 2:
            L.write_miss_table(mis / "daily_false_wakes.csv", rows + [window_row(9, 0.3)])
    L.cmd_queue(ns, player=player, input_fn=lambda _p, it=iter(["n", "q"]): next(it), out=lambda *_: None)
    rows = L.read_miss_table(mis / "daily_false_wakes.csv")
    assert len(rows) == 3 and [r["label"] for r in rows].count("noise") == 1


def test_extract_day_filter_and_autolabel_day_rows_without_label(tmp_path):
    rec = make_tree(tmp_path)
    ns = make_ns(tmp_path, rec)
    ns.day = "2020-01-01"
    L.cmd_extract(ns, X.LocalFetcher(rec), out=lambda *_: None)
    assert L.read_table(Path(ns.dataset_root) / "live" / "live_wakes.csv") == []
    ns.day = L.local_day(L.dir_stamp(T0))
    L.cmd_extract(ns, X.LocalFetcher(rec), out=lambda *_: None)
    n1 = len(L.read_table(Path(ns.dataset_root) / "live" / "live_wakes.csv"))
    L.cmd_extract(ns, X.LocalFetcher(rec), out=lambda *_: None)                           # idempotent
    assert n1 > 0 and len(L.read_table(Path(ns.dataset_root) / "live" / "live_wakes.csv")) == n1
