"""Tests of kivi_usage.py (no audio; tiny label tables in a temporary dataset)."""
import math
from datetime import datetime, timezone

import kivi_live_wakes as L
import kivi_usage as U


def ep(iso):
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()


def stamp(iso):
    return L.dir_stamp(ep(iso))


def live_row(iso, device="c3-b", label="true", speaker="spk01", trigger="wake", source="owner", model="oye_kivi_next",
             conf="1.00"):
    r = {k: "" for k in L.FIELDS}
    r.update(run_id=f"Kiwi/{iso}", device=device, model=model, utc=stamp(iso), wake_len="10.0", stt_len="3.0",
             label=label, speaker=speaker, trigger=trigger, label_source=source, label_conf=conf, speaker_conf=conf)
    return r


def test_speaker_and_source_classes():
    assert [U.spk_class(s) for s in ("spk01", "spk02", "spk-other", "unknown", "")] == \
        ["spk01", "spk02", "other", "unknown", "unknown"]
    assert U.source_class("owner-button") == "owner" and U.source_class("auto-v1") == "auto" and U.source_class("") == "none"
    assert U.era("oye_kivi_ctx2") == "v6.2" and U.era("oye_kivi_next") == "v5"


def test_live_class():
    assert U.live_class(live_row("2026-11-10T10:00:00")) == ("call", "owner")
    assert U.live_class(live_row("2026-11-10T10:00:00", label="false", source="")) == ("false", "none")
    assert U.live_class(live_row("2026-11-10T10:00:00", label="", source="")) == ("uncertain", "none")
    assert U.live_class(live_row("2026-11-10T10:00:00", label="unclear", source="auto-v1", conf="0.5")) == \
        ("uncertain", "auto")
    assert U.live_class(live_row("2026-11-10T10:00:00", trigger="button", label="false")) == ("button-false", "owner")
    assert U.live_class(live_row("2026-11-10T10:00:00", trigger="button", label="", source="")) == \
        ("button-unlabelled", "none")


def test_unique_attempts_and_catch():
    rows = [{"device": "c3-b", "t": 100.0, "speaker": "", "tier": "P3", "sources": "P3+P4", "table": "a", "id": "1"},
            {"device": "c3-b", "t": 102.5, "speaker": "spk02", "tier": "", "sources": "", "table": "b", "id": "2"},
            {"device": "c3-b", "t": 110.0, "speaker": "spk01", "tier": "", "sources": "", "table": "b", "id": "3"},
            {"device": "c3-a", "t": 101.0, "speaker": "spk01", "tier": "", "sources": "", "table": "b", "id": "4"}]
    u = U.unique_attempts(rows)
    assert len(u) == 3
    b = [a for a in u if a["device"] == "c3-b"]
    assert b[0]["speaker"] == "spk02" and b[0]["tiers"] == {"P3", "P4"} and len(b[0]["refs"]) == 2
    assert U.caught_by_live(b[1], {"c3-b": [119.0]}) and not U.caught_by_live(b[1], {"c3-b": [121.0], "c3-a": [110.0]})


def test_exposure_and_union_hours():
    t0, t1 = ep("2026-11-10T16:00:00"), ep("2026-11-10T18:00:00")          # 17-19 Madrid: 1 h day, 1 h evening
    e = U.exposure(["c3-a"], t0, t1, lambda d, t: "oye_kivi_next")
    assert math.isclose(e[("c3-a", "v5", "2026-11-10", False)], 1.0) and math.isclose(e[("c3-a", "v5", "2026-11-10", True)], 1.0)
    h = U.union_hours([(t0, t0 + 1800), (t0 + 900, t0 + 3600 + 900)])       # overlapping pieces count once
    assert math.isclose(sum(h.values()), 1.25) and math.isclose(h[("2026-11-10", True)], 0.25)


def test_intervals():
    lo, hi = U.clopper_pearson(8, 10)
    assert 0.44 < lo < 0.45 and 0.97 < hi < 0.98
    assert U.clopper_pearson(0, 5)[0] == 0.0 and math.isnan(U.clopper_pearson(0, 0)[0])
    lo, hi = U.poisson_ci(0, 2.0)
    assert lo == 0.0 and math.isclose(hi, 3.689 / 2, rel_tol=1e-3)


def test_analyse_end_to_end(tmp_path):
    root = tmp_path / "real"
    (root / "live").mkdir(parents=True)
    (root / "live-misses").mkdir()
    (root / "labelq").mkdir()
    rows = [live_row("2026-11-10T18:00:00"),                                       # call, 19:00 Madrid evening
            live_row("2026-11-10T18:30:00", label="false", speaker="unknown"),     # false wake, evening
            live_row("2026-11-10T10:00:00", label="", speaker="", source=""),     # uncertain
            live_row("2026-11-10T11:00:00", trigger="button", label="false")]      # button session
    L.write_table(root / "live" / "live_wakes.csv", rows)
    win = []
    for i, (iso, label, spk) in enumerate([("2026-11-10T18:00:05", "attempt", "spk01"),     # caught (live within 10 s)
                                           ("2026-11-10T19:00:00", "attempt", "spk02"),     # missed
                                           ("2026-11-10T20:00:00", "", "")], 1):           # pending, no auto label
        r = {k: "" for k in L.MISS_FIELDS}
        r.update(id=f"m{i}", device="c3-b", utc=stamp(iso), label=label, speaker=spk, clip=f"x/{i}.wav")
        win.append(r)
    L.write_miss_table(root / "live-misses" / "miss_candidates.csv", win)
    cells, expo, scan, meta = U.analyse(root, tmp_path, until=ep("2026-11-10T22:00:00"))
    res = U.build(cells, expo, scan, meta)
    b = res["by_tv"]["c3-b"]
    assert (b["caught"], b["missed"], b["false_wakes"], b["false_wakes_evening"], b["button"], b["live_uncertain"]) == \
        (1, 1, 1, 1, 1, 1)
    assert b["pending_attempt_like"] == 1 and math.isclose(b["catch_rate_worst"], 1 / 3)
    assert res["by_tv_speaker"]["c3-b|spk02"]["missed"] == 1
    assert meta["attempts_with_live_wake_within_10s"] == 1
    md = U.render(res)
    assert "| c3-b | 1 | 1 (0) |" in md
