"""Tests of kivi_replay_mine.py. Synthetic audio only; the three real model files are used for the streaming parts.

    cd scripts/voice-assistant/wakeword && OMP_NUM_THREADS=1 \
        $KIVI_HOME/.venv/bin/python -m pytest test_kivi_replay_mine.py -q
"""
import csv
import os
import sys
from argparse import Namespace
from datetime import date
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kivi_extract as X  # noqa: E402
import kivi_live_wakes as L  # noqa: E402
import kivi_real_common as C  # noqa: E402
import kivi_replay_mine as R  # noqa: E402

RATE = C.RATE
T0 = 1_794_265_200.0 - 600            # 2026-11-09T22:50Z = 23:50 CET on the 9th (a test day); midnight 600 s later


def noise(sec, seed=1, amp=2500):
    return (np.random.default_rng(seed).standard_normal(int(sec * RATE)) * amp).astype(np.int16)


def make_piece(ds, dev, name, start, x, part):
    rel = f"negatives-v5era/{dev}/{name}.wav"
    (ds / "negatives-v5era" / dev).mkdir(parents=True, exist_ok=True)
    C.write_wav_new(ds / rel, x)
    C.append_jsonl(ds / "negatives-v5era" / "manifest.jsonl", [C.make_row(
        device=dev, source=C.source_label(dev), label="negative", kind="negative", clip=rel, split=part,
        start_utc=X.utc_str(start), end_utc=X.utc_str(start + len(x) / RATE))])


@pytest.fixture(scope="module")
def runtime():
    return R.load_runtime()


def test_cluster_events_detections_and_overlap():
    t = np.arange(1, 101) * 0.08
    p = np.zeros((100, 3), np.float32)
    p[10, 2], p[11, 2], p[12, 1] = 0.5, 0.99, 0.7        # one event: v5 peak 0.99 at step 11
    p[40, 0] = 0.4                                        # 2.3 s later is too far (gap > 1.5 s): its own event
    p[70, 1], p[71, 1] = 0.35, 0.36
    ev = R.cluster_events(t, p)
    assert len(ev) == 3
    assert ev[0]["peak"] == pytest.approx([0.0, 0.7, 0.99]) and ev[0]["t"] == pytest.approx(t[11]) and ev[0]["t0"] == pytest.approx(t[10])
    assert ev[1]["peak"][0] == pytest.approx(0.4) and ev[1]["t"] == pytest.approx(t[40])          # no v5: time of the highest
    assert R.cluster_events(t, p, merge_s=2.3)[0]["t1"] == pytest.approx(t[40])                    # merge window is a parameter
    # detections: server rule, strictly above the threshold, 2 s refractory
    q = np.zeros(100, np.float32)
    q[[5, 6, 10, 40, 41]] = 0.99
    tt = np.arange(1, 101) * 0.08
    assert R.detections_of(tt, q, 0.98) == [pytest.approx(tt[5]), pytest.approx(tt[40])]          # 10 is within 2 s of 5
    assert R.detections_of(tt, q, 0.99) == []
    # evening hours: a piece from 17:00 to 19:30 Madrid on 2026-11-09 (16:00Z to 18:30Z) has 1.5 h in 18-24
    a = X.parse_utc("2026-11-09T16:00:00.000Z").timestamp()
    assert R.local_overlap_hours(a, a + 9000) == pytest.approx(1.5)
    assert R.local_overlap_hours(a, a + 3 * 86400) == pytest.approx(18.0)                         # three evenings of 6 h
    assert R.local_date(a) == "2026-11-09"


def test_window_bounds_retry_rank_and_selection():
    n = 100 * RATE
    assert R.window_bounds(50, 0, n) == (47 * RATE, 53 * RATE)
    assert R.window_bounds(1, 0, n) == (0, 6 * RATE)                                       # shifted to stay inside
    assert R.window_bounds(99.5, 0, n) == (94 * RATE, 100 * RATE)
    mk = lambda dev, t, peak, part="mine-v5era": {"device": dev, "part": part, "t_abs": t, "peak": peak}   # noqa: E731
    pool = [mk("c3-a", 100, [0.1, 0.6, 0.9]), mk("c3-a", 120, [0.0, 0.1, 0.55]), mk("c3-b", 100, [0.0, 0.0, 0.8]),
            mk("c3-a", 103, [0.0, 0.0, 0.5])]            # 3 s after the first: the same utterance, not evidence for it
    live, stt = {"c3-a": [140.0]}, {"c3-a": [150.0], "c3-b": [101.0, 140.0]}
    R.rank_pool(pool, live, stt)
    assert pool[0]["retry"]["kind"] == "cand" and pool[0]["retry"]["dt_s"] == 20.0          # the other candidate 20 s later; live at +40 is out
    assert pool[1]["retry"]["kind"] == "live" and pool[1]["retry"]["detail"].startswith("live+20.0s;stt-only+30.0s")
    assert pool[2]["retry"]["kind"] == "stt-only" and pool[2]["retry"]["dt_s"] == 1.0       # +40 s is outside the window
    assert pool[0]["rank"] == pytest.approx(0.9 + 0.25 * 0.6 + 0.01 + 0.1)
    assert pool[1]["rank"] == pytest.approx(0.55 + 0.025 + 0.5)
    # selection: balanced over (part, device), minimum separation on the same device, a short cell yields its turns
    big = [mk(d, 1000 + 20 * i + (50000 if part[0] == "t" else 0), [0, 0, 0.5 + i / 1000], part) | {"rank": 1 - i / 1000, "retry": {"kind": ""}}
           for part in ("mine-v5era", "test2-v5era") for d in ("c3-a", "c3-b") for i in range(30)]
    top = R.select_candidates(big, 40)
    cnt = {k: sum(1 for c in top if (c["part"], c["device"]) == k) for k in {(c["part"], c["device"]) for c in big}}
    assert len(top) == 40 and set(cnt.values()) == {10}
    cells = [c for c in big if (c["part"], c["device"]) != ("test2-v5era", "c3-b") or c["t_abs"] < 51100]
    top = R.select_candidates(cells, 40)
    assert len(top) == 40 and sum(1 for c in top if (c["part"], c["device"]) == ("test2-v5era", "c3-b")) == 5
    close = [mk("c3-a", 100, [0, 0, 0.9]) | {"rank": 2, "retry": {"kind": ""}}, mk("c3-a", 105, [0, 0, 0.8]) | {"rank": 1, "retry": {"kind": ""}},
             mk("c3-b", 105, [0, 0, 0.7]) | {"rank": 0.5, "retry": {"kind": ""}}]
    assert [c["rank"] for c in R.select_candidates(close, 40)] == [2, 0.5]                  # 5 s apart on the same device: one only


def test_chunked_scores_equal_the_continuous_stream(tmp_path, runtime):
    """Ranges started warm_s earlier give the scores of one continuous pass (the pass is parallelised on this)."""
    x = np.concatenate([noise(40, 1), noise(40, 2, 6000), noise(40, 3, 800)])
    p = tmp_path / "a.wav"
    C.write_wav_new(p, x)
    full_t, full_p = R.stream_range(*runtime, p, 0, len(x), warm_s=0)
    from eval_stream import stream
    probs, times = stream(runtime[0], runtime[1], x)
    ref = np.stack([probs[n] for n in R.NAMES], axis=1)
    assert len(full_t) == len(times) and np.allclose(full_t, times) and np.array_equal(full_p, ref)       # same as eval_stream.stream
    step = 40 * RATE // R.CHUNK * R.CHUNK
    parts = [R.stream_range(*runtime, p, s0, min(len(x), s0 + step), warm_s=30) for s0 in range(0, len(x), step)]
    ct, cp = np.concatenate([a for a, _ in parts]), np.concatenate([b for _, b in parts])
    assert len(ct) == len(full_t) and np.allclose(ct, full_t)
    assert np.abs(cp - full_p).max() < 1e-6


def test_score_collect_report_and_candidates_end_to_end(tmp_path, monkeypatch, capsys):
    ds = tmp_path / "ds"
    make_piece(ds, "c3-a", "1-01", T0, noise(130, 4), "test2-v5era")
    make_piece(ds, "c3-b", "2-01", T0 + 700, noise(70, 5), "mine-v5era")
    a = Namespace(dataset_root=str(ds), set="negatives-v5era", out="scores/v5era-replay", jobs=1, chunk_s=60.0, warm_s=20.0,
                  bench_s=None, floor=0.5, limit=40, rewrite=False, devices=None, devices_file=None, from_dir=None, context="x",
                  namespace="x", pod="x", container=None, recordings_root="/x")
    lines = []
    R.cmd_score(a, out=lines.append)
    assert any("pass finished" in ln for ln in lines) and (ds / "scores/v5era-replay/score_meta.json").exists()
    assert len(list((ds / "scores/v5era-replay/chunks").glob("*.npz"))) == 3 + 2
    lines.clear()
    R.cmd_score(a, out=lines.append)                                                    # resumable: nothing left to do
    assert any("0 to do" in ln for ln in lines)
    summ = R.cmd_report(a, out=lambda *_: None)
    assert summ["cells"]["all|all"]["hours"] == pytest.approx((130 + 70) / 3600, abs=1e-3)
    assert summ["cells"]["c3-a|test2-v5era"]["evening_hours"] == pytest.approx(130 / 3600, abs=1e-3)       # 23:50 CET is evening
    assert summ["hours_by_device_day_part"]["c3-a 2026-11-09 test2-v5era"] > 0
    # candidates from fabricated events on the real pieces (noise gives none)
    res = R.collect(ds, "negatives-v5era", "scores/v5era-replay", 60.0)
    ev = lambda t, pk: {"t_abs": t, "peak": pk}      # noqa: E731
    res[0]["events"] = [ev(T0 + 30, [0.1, 0.8, 0.9]), ev(T0 + 60, [0.0, 0.2, 0.6]), ev(T0 + 100, [0, 0, 0.99])]   # last: v5 fired
    res[1]["events"] = [ev(T0 + 700 + 1, [0.0, 0.3, 0.7])]                                 # 1 s into the piece: window shifted
    monkeypatch.setattr(R, "collect", lambda *a_, **k: res)
    monkeypatch.setattr(R, "stt_only_times", lambda *a_, **k: {"c3-a": [T0 + 50]})
    monkeypatch.setattr(L, "devices_prefix", lambda a_: {"c3-a": "bd8256", "c3-b": "fda18f"})
    (ds / "live").mkdir()
    L.write_table(ds / "live" / "live_wakes.csv", [])
    rows, pool, chosen = R.cmd_candidates(a, fetcher=object(), out=lambda *_: None)
    assert len(pool) == 3 and len(rows) == 3                                               # the v5-fired event is not a miss
    assert [r["id"] for r in rows] == ["mc001", "mc002", "mc003"] and rows[0]["peak_v5"] == 0.9 and rows[0]["retry"] == "stt-only"
    tbl = list(csv.DictReader(open(ds / "live-misses" / "miss_candidates.csv")))
    assert list(tbl[0]) == list(R.MISS_FIELDS) and tbl[0]["label"] == "" and tbl[0]["speaker"] == ""
    for r in tbl:
        w = ds / "live-misses" / r["clip"]
        assert w.exists() and (w.stat().st_size - 44) // 2 == 6 * RATE and oct(w.stat().st_mode & 0o777) in ("0o600", "0o644")
    assert oct((ds / "live-misses").stat().st_mode & 0o777) == "0o700"
    with pytest.raises(SystemExit):                                                       # labels are never overwritten
        R.cmd_candidates(a, fetcher=object(), out=lambda *_: None)
