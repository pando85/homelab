"""Tests of kivi_label_queue.py and the plan group of kivi_live_wakes.py queue (no audio models: fakes)."""
import csv
import json
from datetime import datetime, timezone

import numpy as np

import kivi_label_queue as Q
import kivi_live_wakes as L
import kivi_real_common as C


def stamp(iso):
    return L.dir_stamp(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())


def ep(iso):
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()


def test_split_and_evening():
    assert Q.split_of("2026-11-05") == "train" and Q.split_of("2026-11-07") == "train"      # grandfathered
    assert Q.split_of("2026-11-09") == "test" and Q.split_of("2026-11-10") == "train"
    assert Q.madrid(ep("2026-11-10T17:30:00")) == ("2026-11-10", True)                     # 18:30 local
    assert Q.madrid(ep("2026-11-10T23:30:00")) == ("2026-11-11", False)                    # 00:30 local


def test_merge_detections_chains_and_keeps_max():
    m = Q.merge_detections([(10.0, "v5", 0.4), (11.5, "v6", 0.9), (13.0, "v5", 0.6), (20.0, "p3a", 0.5)])
    assert len(m) == 2
    assert m[0]["t"] == 11.5 and m[0]["scores"] == {"v5": 0.6, "v6": 0.9}
    assert m[1]["scores"] == {"p3a": 0.5}


def test_decide_window_and_owner_rule():
    base = {"phrase": False, "weak": False, "oye_like": False, "kivi_near": False, "n_words": 0, "mean_prob": 0.0,
            "speech_mid_s": 0.0}
    assert Q.decide_window(base) == ("noise", 0.95)
    assert Q.decide_window(dict(base, speech_mid_s=1.0))[0] == "unclear"
    assert Q.decide_window(dict(base, n_words=4, mean_prob=0.8)) == ("other-speech", 0.75)
    assert Q.decide_window(dict(base, n_words=4, mean_prob=0.3))[0] == "unclear"
    assert Q.decide_window(dict(base, n_words=3, oye_like=True))[0] == "unclear"
    assert Q.decide_window(dict(base, phrase=True, n_words=2))[0] == "attempt"
    assert not Q.to_owner_window("noise", 0.95) and not Q.to_owner_window("other-speech", 0.75)
    assert Q.to_owner_window("attempt", 0.95) and Q.to_owner_window("unclear", 0.5)
    assert Q.to_owner_window("other-speech", 0.6)


def test_to_owner_live_policy():
    r = {k: "" for k in L.FIELDS}
    assert Q.to_owner_live(dict(r)) == "unlabelled"
    assert Q.to_owner_live(dict(r, label="false", label_source="auto-v1", label_conf="0.75")) is None
    assert Q.to_owner_live(dict(r, label="true", label_source="auto-v1", label_conf="0.95", speaker="spk01")) == "call"
    assert Q.to_owner_live(dict(r, label="unclear", label_source="auto-v1")) == "uncertain"
    assert Q.to_owner_live(dict(r, label="true", label_source="owner", speaker="spk02")) is None
    assert Q.to_owner_live(dict(r, label="false", label_source="owner-button")) is None


def test_audit_sample_is_seeded_and_rounded_up():
    refs = [f"window|lq-{i:05d}" for i in range(41)]
    s1, s2 = Q.audit_sample(refs), Q.audit_sample(list(reversed(refs)))
    assert s1 == s2 and len(s1) == 3                      # ceil(0.05 * 41)
    assert Q.audit_sample([]) == set() and len(Q.audit_sample(refs[:3])) == 1


def test_p3_order_c3b_evenings_then_even_days_then_score():
    mk = lambda dev, ev, split, s, t: {"device": dev, "evening": ev, "split": split, "scores": {"v5": s}, "t": t}  # noqa: E731
    cs = [mk("c3-a", False, "train", 0.9, 1), mk("c3-b", True, "test", 0.9, 2), mk("c3-b", True, "train", 0.6, 3),
          mk("c3-a", True, "train", 0.99, 4), mk("c3-b", False, "train", 0.7, 5), mk("c3-b", True, "train", 0.8, 6)]
    got = [c["t"] for c in sorted(cs, key=Q.p3_order)]
    assert got == [6, 3, 2, 4, 5, 1]


def _piece(tmp_path, dev, start_iso, seconds, name):
    rel = f"negatives-daily/{dev}/{name}.wav"
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(len(name))
    C.write_wav_new(p, (rng.standard_normal(int(seconds * 16000)) * 300).astype(np.int16))
    return {"clip": rel, "device": dev, "start": ep(start_iso), "n": int(seconds * 16000), "part": "d"}


def test_candidates_tiers_and_dedup(tmp_path):
    pc_b = _piece(tmp_path, "c3-b", "2026-11-10T17:00:00", 3600, "b1")
    pc_a = _piece(tmp_path, "c3-a", "2026-11-10T09:00:00", 3600, "a1")
    t0b, t0a = pc_b["start"], pc_a["start"]
    moments = [{"device": "c3-b", "t": t0b + 100, "scores": {"v5": 0.7, "v6": 0.2}},     # P3, evening
               {"device": "c3-b", "t": t0b + 200, "scores": {"p3a": 0.35}},             # P4: a live run 20 s later
               {"device": "c3-b", "t": t0b + 300, "scores": {"v6": 0.6}},               # labelled window 2 s away
               {"device": "c3-a", "t": t0a + 400, "scores": {"v5": 0.55}},              # P3, day
               {"device": "c3-a", "t": t0a + 500, "scores": {"v5": 0.4}}]               # neither
    live = [{k: "" for k in L.FIELDS} | {"run_id": "Kiwi/1", "device": "c3-b", "utc": L.dir_stamp(t0b + 215), "wake_len": "5.00",
                                         "stt_len": "2.00"}]
    fair = [{"device": "c3-b", "utc": L.dir_stamp(t0b + 101)}]                            # the same moment as the first
    cands = Q.candidates(tmp_path, moments, fair, live, [pc_b, pc_a])
    assert [c["tier"] for c in cands] == ["P1", "P3", "P3", "P3", "P4"]
    assert cands[1]["device"] == "c3-b" and cands[1]["evening"]                          # c3-b evening first
    windows = [("c3-b", t0b + 302, "other-speech", "owner", "x.csv", "w1")]
    kept, why = Q.dedup(cands, windows, Q.live_spans(live))
    assert [(c["tier"], sorted(c["sources"])) for c in kept] == [("P1", ["P1", "P3"]), ("P3", ["P3"]), ("P4", ["P4"])]
    assert why["P3: labelled window within 3 s"] == 1 and why["P3: same moment as an earlier candidate (P1)"] == 1


class FakeLabeller:
    def __init__(self, labels):
        self.labels = iter(labels)

    def label(self, pcm):
        lab, conf, spk = next(self.labels)
        return {"label": lab, "conf": conf, "speaker": spk, "speaker_conf": 0.8 if spk else 0.0, "evidence": "ph=0"}


def test_build_plan_queue_and_report(tmp_path, monkeypatch):
    root = tmp_path / "real"
    (root / "live").mkdir(parents=True)
    (root / "live-misses").mkdir()
    pc = _piece(root, "c3-b", "2026-11-10T17:00:00", 1200, "b1")
    mf = root / "negatives-daily" / "manifest.jsonl"
    mf.write_text(json.dumps({"clip": pc["clip"], "device": "c3-b", "start_utc": "2026-11-10T17:00:00.000Z", "split": "d"}) + "\n")
    work = tmp_path / "kh" / "labelq"
    work.mkdir(parents=True)
    monkeypatch.setattr(Q, "WORK", work)
    monkeypatch.setattr(Q, "KH", tmp_path / "kh")
    t0 = pc["start"]
    moments = [{"device": "c3-b", "t": t0 + 60 * k, "scores": {"v5": 0.6 + 0.01 * k}} for k in range(1, 6)]
    (work / "moments.json").write_text(json.dumps({"moments": moments}))
    live_rows = [{k: "" for k in L.FIELDS} | {"run_id": f"Kiwi/{i}", "device": "c3-a", "utc": stamp(f"2026-11-10T1{i}:00:00"),
                                             "wake_len": "4.00", "stt_len": "3.00", "label": lab, "label_source": src,
                                             "label_conf": conf, "speaker": spk}
                 for i, (lab, src, conf, spk) in enumerate([("true", "auto-v1", "0.95", "spk02"), ("false", "auto-v1", "0.75", "unknown"),
                                                            ("true", "owner", "1.00", "spk01"), ("unclear", "auto-v1", "0.50", "")])]
    L.write_table(root / "live" / "live_wakes.csv", live_rows)
    ns = type("A", (), {"dataset_root": str(root), "dry_run": False})()
    fake = FakeLabeller([("noise", 0.95, ""), ("attempt", 0.95, "spk02"), ("other-speech", 0.75, "spk01"),
                         ("unclear", 0.5, ""), ("other-speech", 0.75, "unknown")])
    Q.cmd_build(ns, labeller=fake)
    table = L.read_miss_table(root / "live-misses" / Q.TABLE)
    assert len(table) == 5 and all(r["split"] == "train" and r["tier"] == "P3" for r in table)
    assert [r["label"] for r in table] == ["noise", "", "other-speech", "", "other-speech"]
    assert table[2]["speaker"] == "spk01" and table[2]["label_source"] == "auto-v1"
    assert all((root / "live-misses" / r["clip"]).exists() for r in table)
    with open(root / "labelq" / "plan.csv", newline="") as f:
        plan = list(csv.DictReader(f))
    with open(root / "labelq" / "key.csv", newline="") as f:
        key = {(k["kind"], k["ref"]): k for k in csv.DictReader(f)}
    assert "auto_label" not in plan[0] and "audit" not in plan[0]                        # the plan is blind
    tiers = [p["tier"] for p in plan]
    assert tiers == sorted(tiers, key=Q.TIERS.index)
    owner_refs = {p["ref"] for p in plan}
    assert {"Kiwi/0", "Kiwi/3"} <= owner_refs and "Kiwi/2" not in owner_refs           # call + uncertain; owner kept
    assert table[1]["id"] in owner_refs and table[3]["id"] in owner_refs                 # attempt, unclear
    audits = [k for k in key.values() if k["audit"] == "1"]
    assert len(audits) == 1 and (audits[0]["ref"] in owner_refs)                         # 5 % of 4 trusted -> 1
    # the queue walks the plan first, then nothing else asks the same items again
    items = L.queue_items(root)
    assert items and all(it["group"] == L.G_PLAN for it in items)
    assert len(items) == len(plan)
    # owner labels one window: label_source becomes owner, the item leaves the queue
    w = next(it for it in items if it["kind"] == "window")
    L.save_miss_label(w["table"], w["row"]["id"], "attempt", "spk02")
    after = {r["id"]: r for r in L.read_miss_table(root / "live-misses" / Q.TABLE)}
    assert after[w["row"]["id"]]["label_source"] == "owner" and after[w["row"]["id"]]["split"] == "train"
    assert len(L.queue_items(root)) == len(plan) - 1
    # a second build adds nothing new and keeps the plan and the owner label
    Q.cmd_build(ns, labeller=FakeLabeller([]))
    assert len(L.read_miss_table(root / "live-misses" / Q.TABLE)) == 5
    ns2 = type("A", (), {"dataset_root": str(root), "json": None})()
    Q.cmd_report(ns2)
    Q.cmd_audit(ns2)


def test_old_tables_keep_their_header(tmp_path):
    t = tmp_path / "x.csv"
    L.write_miss_table(t, [{k: "" for k in L.MISS_FIELDS} | {"id": "w1", "device": "c3-a", "utc": "20261110T100000.000Z"}])
    L.save_miss_label(t, "w1", "noise", "")
    with open(t, newline="") as f:
        assert next(csv.reader(f)) == list(L.MISS_FIELDS)
    rows = L.read_miss_table(t)
    assert rows[0]["label"] == "noise" and "label_source" not in rows[0]


def test_plan_key_orders_p3_blocks():
    it = lambda tier, dev, ev, split, utc: {"tier": tier, "device": dev, "evening": ev, "split": split, "utc": utc}  # noqa: E731
    items = [(it("P3", "c3-a", "0", "train", "b"), 0.99), (it("P3", "c3-b", "1", "test", "c"), 0.9),
             (it("P1", "c3-a", "0", "test", "z"), 0.0), (it("P3", "c3-b", "1", "train", "d"), 0.6),
             (it("P4", "c3-b", "1", "train", "a"), 0.4), (it("P3", "c3-a", "1", "train", "e"), 0.7)]
    got = [x["utc"] for x, s in sorted(items, key=lambda xs: Q.plan_key(*xs))]
    assert got == ["z", "d", "c", "e", "b", "a"]
