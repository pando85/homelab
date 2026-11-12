"""Tests of kivi_room_grid.py (src_sample0 of room pieces copied before the manifest recorded it). Synthetic times only.

    cd scripts/voice-assistant/wakeword && $KIVI_HOME/.venv/bin/python -m pytest test_kivi_room_grid.py -q
"""
import json
import sys
from argparse import Namespace
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kivi_extract as X  # noqa: E402
import kivi_real_common as C  # noqa: E402
import kivi_room_grid as G  # noqa: E402

RATE = C.RATE
MTIME = 1_794_300_000                       # whole second (the pod's `stat %Y`), 2026-11-10T09:20Z
N = 7_654_321                               # samples of the wake file (any count: odd, not on a 160 or 1280 boundary)


def piece_times(s0, s1, mtime=MTIME, n=N):
    """Manifest times of a piece [s0, s1) of a file, the way kivi_negatives.py writes them (utc_str of the epoch times)."""
    start = mtime - n / RATE
    return X.utc_str(start + s0 / RATE), X.utc_str(start + s1 / RATE)


@pytest.mark.parametrize("s0,s1,method", [
    (G.TRIM_S0, N - G.BOUNDARY, "start-trim"),                    # a whole closed file
    (G.BOUNDARY, 3_000_000, "start-followup"),                    # a follow-up file cut before a press
    (2_345_678, N - G.BOUNDARY, "end-boundary"),                  # cut after a press, to the end of the file
])
def test_exact_classes(s0, s1, method):
    t0, t1 = piece_times(s0, s1)
    g = G.derive(t0, t1, s1 - s0, N, MTIME)
    assert g == {"src_sample0": s0, "lo": s0, "hi": s0, "method": method}


def test_midnight_cuts_are_exact():
    midnight = 1_794_265_200                    # 2026-11-10 00:00 Europe/Madrid (23:00Z)
    mtime = midnight + 300                      # the file (478 s) spans midnight
    s_mid = int(round((midnight - (mtime - N / RATE)) * RATE))
    t0, t1 = piece_times(s_mid, N - G.BOUNDARY, mtime)
    assert G.derive(t0, t1, N - G.BOUNDARY - s_mid, N, mtime)["src_sample0"] == s_mid      # end-boundary first, same value
    t0, t1 = piece_times(s_mid, s_mid + 200_000, mtime)
    assert G.derive(t0, t1, 200_000, N, mtime) == {"src_sample0": s_mid, "lo": s_mid, "hi": s_mid, "method": "start-midnight"}
    t0, t1 = piece_times(1_000_003, s_mid, mtime)
    assert G.derive(t0, t1, s_mid - 1_000_003, N, mtime)["method"] == "end-midnight"


def test_press_cut_on_both_sides_is_bracketed_around_the_truth():
    s0, s1 = 2_345_678, 3_456_789
    t0, t1 = piece_times(s0, s1)
    g = G.derive(t0, t1, s1 - s0, N, MTIME)
    assert g["src_sample0"] is None and g["method"] == "bracketed"
    assert g["lo"] <= s0 <= g["hi"] and g["hi"] - g["lo"] <= 18


def test_inconsistent_listing():
    t0, t1 = piece_times(G.TRIM_S0, N - G.BOUNDARY)
    assert G.derive(t0, t1, N - G.BOUNDARY - G.TRIM_S0 + 5000, N, MTIME)["method"] == "inconsistent"   # wrong length


def test_derive_set_writes_grid_rows_for_old_pieces_only(tmp_path):
    ds = tmp_path / "ds"
    (ds / "neg" / "c3-a").mkdir(parents=True)
    rows = []
    for k, (s0, s1, extra) in enumerate([(G.TRIM_S0, N - G.BOUNDARY, {}), (G.BOUNDARY, 300_000, {"src_sample0": G.BOUNDARY})]):
        rel = f"neg/c3-a/{k}.wav"
        C.write_wav_new(ds / rel, np.zeros(s1 - s0, np.int16))
        t0, t1 = piece_times(s0, s1)
        rows.append(C.make_row(device="c3-a", source="tv-c3a", label="negative", kind="negative", clip=rel, split="x",
                               start_utc=t0, end_utc=t1, source_run=f"Kiwi/{k}", **extra))
    rel = "neg/c3-a/9.wav"                                                       # a piece whose file is not listed
    C.write_wav_new(ds / rel, np.zeros(1000, np.int16))
    rows.append(C.make_row(device="c3-a", source="tv-c3a", label="negative", kind="negative", clip=rel, split="x",
                           start_utc=piece_times(0, 1)[0], end_utc=piece_times(0, 1)[1], source_run="Kiwi/9"))
    C.append_jsonl(ds / "neg" / "manifest.jsonl", rows)
    listing = tmp_path / "l.json"
    listing.write_text(json.dumps({"files": {"c3-a": [{"pipeline": "Kiwi", "run": str(k), "size": 44 + 2 * N, "mtime": MTIME}
                                                      for k in (0, 1)]}}))
    a = Namespace(dataset_root=str(ds), set=["neg"], listing=str(listing), save_listing=None, dry_run=False)
    G.cmd_derive(a, out=lambda *_: None)
    got = [json.loads(x) for x in (ds / "neg" / "grid.jsonl").read_text().splitlines()]
    assert [(r["clip"], r["src_sample0"], r["method"]) for r in got] == [
        ("neg/c3-a/0.wav", G.TRIM_S0, "start-trim"), ("neg/c3-a/9.wav", None, "no-listing")]    # the new row is skipped
    import kivi_replay_mine as R
    ps = {p["clip"]: (p["src0"], p["grid"]) for p in R.read_pieces(ds, "neg")}
    assert ps == {"neg/c3-a/0.wav": (G.TRIM_S0, "exact"), "neg/c3-a/1.wav": (G.BOUNDARY, "exact"),
                  "neg/c3-a/9.wav": (None, "unknown")}
