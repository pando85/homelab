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


def _cand(dev, ev, split, scores, t):
    return {"device": dev, "evening": ev, "split": split, "scores": scores, "t": t}


def test_p3_order_train_first_then_band_then_block_then_score():
    # t encodes the expected rank; scores of the current heads (v6.3 is one), v5 never counts
    cs = [_cand("c3-b", True, "test", {"v6.3": 0.99}, 7),                   # test day: after every train item
          _cand("c3-a", False, "train", {"v6.3": 0.95, "v5": 0.5}, 3),      # band 0, c3-a day
          _cand("c3-b", True, "train", {"v5": 0.99, "v6": 0.6}, 5),         # v5 0.99 is ignored: band 2
          _cand("c3-a", True, "train", {"v5": 0.97}, 6),                    # only v5: band 3
          _cand("c3-b", False, "train", {"v6.2": 0.8}, 4),                  # band 1
          _cand("c3-b", True, "train", {"p3a": 0.92}, 1),                   # band 0, c3-b evening
          _cand("c3-b", True, "train", {"p3c": 0.99}, 0),                   # same block, higher score first
          _cand("c3-a", True, "train", {"v6.3": 0.9}, 2)]                   # band 0 (>= 0.9), c3-a evening
    got = [c["t"] for c in sorted(cs, key=Q.p3_order)]
    assert got == [0, 1, 2, 3, 4, 5, 6, 7]


def test_p3_order_v5_only_last_and_band_edges():
    cs = [_cand("c3-b", True, "train", {"v5": 0.99}, 9),
          _cand("c3-b", True, "train", {"v5": 0.6, "v6": 0.4994}, 8),       # current max < 0.5: band 3
          _cand("c3-a", False, "train", {"v6": 0.5}, 3),                    # edges: 0.5 -> band 2
          _cand("c3-a", False, "train", {"v6": 0.7}, 2),                    # 0.7 -> band 1
          _cand("c3-a", False, "train", {"v6": 0.9}, 1)]                    # 0.9 -> band 0
    assert [c["t"] for c in sorted(cs, key=Q.p3_order)] == [1, 2, 3, 8, 9]
    assert [Q.band_of(x) for x in (0.95, 0.9, 0.89, 0.7, 0.69, 0.5, 0.49, 0.0)] == [0, 0, 1, 1, 2, 2, 3, 3]
    assert Q.current_score({"v5": 0.99}) == 0.0 and Q.current_score({"oof": 0.9, "v6.3": 0.6}) == 0.6
    assert "v6.3" in Q.HEADS and Q.HEADS["v6.3"].endswith("oye_kivi_ctx3_uncalibrated.onnx")
    assert Q.HEADS["v6.4"].endswith("oye_kivi_ctx4_uncalibrated.onnx")


def _piece(tmp_path, dev, start_iso, seconds, name, amp=300):
    rel = f"negatives-daily/{dev}/{name}.wav"
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(len(name))
    C.write_wav_new(p, (rng.standard_normal(int(seconds * 16000)) * amp).astype(np.int16))
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


def _item(tier, dev, ev, split, utc):
    return {"tier": tier, "device": dev, "evening": ev, "split": split, "utc": utc}


def test_plan_key_orders_p3_tiers_and_pc():
    items = [(_item("P3", "c3-a", "0", "train", "b"), 0.99), (_item("P3", "c3-b", "1", "test", "c"), 0.9),
             (_item("P1", "c3-a", "0", "test", "z"), 0.0), (_item("P3", "c3-b", "1", "train", "d"), 0.6),
             (_item("P4", "c3-b", "1", "train", "a"), 0.4), (_item("P3", "c3-a", "1", "train", "e"), 0.7),
             (_item("P3", "c3-b", "1", "train", "f"), 0.2),                       # v5-only: last of the train items
             (_item("PC", "c3-b", "0", "train", "g"), 0.95)]
    got = [x["utc"] for x, s in sorted(items, key=lambda xs: Q.plan_key(*xs))]
    # P1; train: band 0 (g PC block 2.5 before b c3-a day), band 1 (e), band 2 (d), band 3 (f); then test (c); P4
    assert got == ["z", "g", "b", "e", "d", "f", "c", "a"]
    pc = [(_item("PC", "c3-b", "0", "train", "p"), 0.8), (_item("P3", "c3-b", "0", "train", "q"), 0.8),
          (_item("P3", "c3-a", "0", "train", "r"), 0.8), (_item("P3", "c3-a", "1", "train", "s"), 0.8)]
    assert [x["utc"] for x, s in sorted(pc, key=lambda xs: Q.plan_key(*xs))] == ["s", "q", "p", "r"]


def _plan_from(root, cands):
    """Window rows (peaks as build writes them) and plan items of the candidates, in a scrambled order."""
    rows, plan = [], []
    for i, c in enumerate(sorted(cands, key=lambda c: (c["t"] * 7919) % 1013), 1):
        utc = L.dir_stamp(c["t"])
        peaks = ";".join(f"{k}={v:.3f}" for k, v in sorted(c["scores"].items()))
        rows.append({k: "" for k in Q.FIELDS} | {"id": f"lq-{i:05d}", "device": c["device"], "utc": utc, "peaks": peaks,
                                                 "split": c["split"], "tier": "P3", "evening": str(int(c["evening"]))})
        plan.append(dict(Q._plan_item("window", f"lq-{i:05d}", "P3", c["device"], utc, c["split"], str(int(c["evening"]))),
                         order=str(i)))
    L.write_miss_table(root / "live-misses" / Q.TABLE, rows)
    L.write_csv_atomic(root / "labelq" / "plan.csv", Q.PLAN_FIELDS, plan)


def _plan_order(root):
    with open(root / "labelq" / "plan.csv", newline="") as f:
        return [(p["device"], Q.utc_epoch(p["utc"])) for p in csv.DictReader(f)]


def test_reorder_gives_the_build_order(tmp_path, monkeypatch):
    root = tmp_path / "real"
    (root / "live-misses").mkdir(parents=True)
    (root / "labelq").mkdir()
    work = tmp_path / "kh" / "labelq"
    work.mkdir(parents=True)
    monkeypatch.setattr(Q, "WORK", work)
    base = ep("2026-11-10T17:00:00")
    cands = []
    for dev, off, sc in [("c3-b", 0, {"v5": 0.99}), ("c3-b", 60, {"v5": 0.6, "v6.3": 0.9496}), ("c3-a", 120, {"v6": 0.75}),
                         ("c3-b", 180, {"v6.2": 0.55, "v5": 0.9}), ("c3-a", 240, {"p3b": 0.97}), ("c3-b", 300, {"v6": 0.5}),
                         ("c3-b", 360, {"v6.3": 0.91}), ("c3-a", 420, {"v5": 0.7}), ("c3-a", 480, {"v6": 0.4}),
                         ("c3-a", 540, {"v6": 0.8996})]:
        for dd in (0, 1, 2, 13):                                           # days 10 and 12 train, 11 and 23 test
            t = base + off + 86400 * dd
            day, ev = Q.madrid(t)
            cands.append({"device": dev, "t": t, "day": day, "evening": ev, "scores": sc, "split": Q.split_of(day)})
    _plan_from(root, cands)
    (work / "moments.json").write_text(json.dumps({"moments": []}))
    ns = type("A", (), {"dataset_root": str(root)})()
    Q.cmd_reorder(ns)
    assert _plan_order(root) == [(c["device"], c["t"]) for c in sorted(cands, key=Q.p3_order)]
    got = _plan_order(root)
    assert [c["split"] for c in sorted(cands, key=Q.p3_order)] == ["train"] * 20 + ["test"] * 20
    # a later scan adds a head the rows were cut without: reorder picks it up from the nearest moment (v5-only -> band 0)
    c = next(c for c in cands if c["scores"] == {"v5": 0.99} and c["split"] == "train")
    assert got.index((c["device"], c["t"])) >= 14                          # band 3 (6 items) is the end of the 20 train items
    (work / "moments.json").write_text(json.dumps({"moments": [
        {"device": c["device"], "t": c["t"] + 0.5, "scores": {"v5": 0.99, "v6.3": 0.97}}]}))
    Q.cmd_reorder(ns)
    assert _plan_order(root)[0] == (c["device"], c["t"])                   # c3-b evening, band 0, the highest score


# ---------------------------------------------------------------------------------------------
# quiet windows: level_dbfs below QUIET_NOISE_DBFS without wake-like evidence is a trusted `noise`
# ---------------------------------------------------------------------------------------------

CLEAN = "ph=0;weak=0;kivi=0;oye=0;score=0.0;ksim=0.3;words=2;wconf=0.4;spm=0.8;auto=unclear/0.45/-"
WAKELIKE = "ph=1;weak=0;kivi=1;oye=1;ksim=1.0"


def test_quiet_rule_boundaries():
    assert Q.QUIET_NOISE_DBFS == -50.0
    assert not Q.is_quiet_noise(-50.0, CLEAN, "unclear", 0.45)                 # exactly -50: to the owner
    assert not Q.is_quiet_noise(-49.9, CLEAN, "unclear", 0.45)
    assert Q.is_quiet_noise(-50.1, CLEAN, "unclear", 0.45)
    assert Q.is_quiet_noise("-180.0", CLEAN, "unclear", 0.4)
    assert not Q.is_quiet_noise("", CLEAN, "unclear", 0.4)                      # no level: no rule
    for ev in ("ph=1;weak=0", "ph=0;weak=1", "ph=0;weak=0;kivi=1", "ph=0;weak=0;kivi=0;oye=1", "ph=0;ksim=0.75;oye=0",
               "ph=0;ksim=0.9", ""):
        assert not Q.is_quiet_noise(-60.0, ev, "unclear", 0.5), ev             # wake-like or never transcribed: owner
    assert Q.is_quiet_noise(-60.0, "ph=0;weak=0;kivi=0;oye=0;ksim=0.74", "unclear", 0.5)
    assert not Q.is_quiet_noise(-60.0, CLEAN, "attempt", 0.92)                  # a quiet real call is not negated
    assert not Q.is_quiet_noise(-60.0, CLEAN, "other-speech", 0.75)             # already trusted: keeps its label
    assert Q.is_quiet_noise(-60.0, CLEAN, "other-speech", 0.6)                  # to the owner by confidence: noise
    r = Q.quiet_override({"label": "unclear", "conf": 0.45, "speaker": "spk01", "speaker_conf": 0.7, "evidence": CLEAN}, -61.0)
    assert (r["label"], r["conf"], r["speaker"], r["quiet"]) == ("noise", 0.9, "", True) and r["evidence"].endswith(";quiet=1")
    res = {"label": "unclear", "conf": 0.5, "speaker": "", "speaker_conf": 0.0, "evidence": "ph=0;oye=1"}
    assert Q.quiet_override(res, -70.0) is res


def test_build_quiet_windows_are_trusted_noise(tmp_path, monkeypatch):
    root = tmp_path / "real"
    (root / "live").mkdir(parents=True)
    (root / "live-misses").mkdir()
    pc = _piece(root, "c3-b", "2026-11-10T17:00:00", 1200, "b1", amp=10)       # about -70 dBFS
    (root / "negatives-daily" / "manifest.jsonl").write_text(
        json.dumps({"clip": pc["clip"], "device": "c3-b", "start_utc": "2026-11-10T17:00:00.000Z", "split": "d"}) + "\n")
    work = tmp_path / "kh" / "labelq"
    work.mkdir(parents=True)
    monkeypatch.setattr(Q, "WORK", work)
    monkeypatch.setattr(Q, "KH", tmp_path / "kh")
    moments = [{"device": "c3-b", "t": pc["start"] + 60 * k, "scores": {"v5": 0.6 + 0.01 * k}} for k in range(1, 4)]
    (work / "moments.json").write_text(json.dumps({"moments": moments}))
    L.write_table(root / "live" / "live_wakes.csv", [])

    class Fake:
        def __init__(self):
            self.labels = iter([("unclear", 0.4, ""), ("attempt", 0.92, "spk02"), ("unclear", 0.5, "")])
            self.evidence = iter([CLEAN, WAKELIKE, "ph=0;weak=0;kivi=0;oye=1;ksim=0.5"])

        def label(self, pcm):
            lab, conf, spk = next(self.labels)
            return {"label": lab, "conf": conf, "speaker": spk, "speaker_conf": 0.0, "evidence": next(self.evidence)}

    ns = type("A", (), {"dataset_root": str(root), "dry_run": False})()
    Q.cmd_build(ns, labeller=Fake())
    table = L.read_miss_table(root / "live-misses" / Q.TABLE)
    assert [float(r["level_dbfs"]) < -50 for r in table] == [True] * 3
    assert [(r["label"], r["label_source"]) for r in table] == [("noise", "auto-quiet"), ("", ""), ("", "")]
    assert table[0]["label_conf"] == "0.90" and "quiet=1" in table[0]["auto_evidence"]
    with open(root / "labelq" / "key.csv", newline="") as f:
        key = {k["ref"]: k for k in csv.DictReader(f)}
    # one trusted label, 5 % rounded up = 1 audited: it keeps its label in the table and stays in the plan
    assert key[table[0]["id"]]["why"] == "audit" and key[table[0]["id"]]["audit"] == "1"
    assert key[table[1]["id"]]["why"] == "attempt" and key[table[2]["id"]]["why"] == "unclear"
    assert len(L.queue_items(root)) == 3


def _quiet_root(tmp_path, n_quiet=41):
    """A window table with pending quiet rows (train and test) plus the rows the rule must leave alone."""
    root = tmp_path / "real"
    (root / "live").mkdir(parents=True)
    (root / "live-misses").mkdir()
    (root / "labelq").mkdir()
    rows, plan, key = [], [], []

    def add(i, level, label="", src="", ev=CLEAN, split="train", auto="unclear", why="unclear"):
        rid = f"lq-{i:05d}"
        rows.append({k: "" for k in Q.FIELDS} | {"id": rid, "device": "c3-b", "utc": f"20261110T1{i % 10}0000.000Z",
                                                  "clip": f"labelq/{rid}.wav", "level_dbfs": str(level), "label": label,
                                                  "label_source": src, "label_conf": "1.00" if src == "owner" else "",
                                                  "speaker": "spk01" if src == "owner" else "", "split": split, "tier": "P3",
                                                  "evening": "1", "auto_evidence": ev})
        plan.append(Q._plan_item("window", rid, "P3", "c3-b", f"20261110T1{i % 10}0000.000Z", split, "1"))
        key.append({"kind": "window", "ref": rid, "tier": "P3", "auto_label": auto, "auto_conf": "0.40", "auto_speaker": "",
                    "audit": "0", "why": why})
        return rid

    for i in range(n_quiet):
        add(i, -55.0 - i / 10, split="train" if i % 3 else "test")
    ids = {"edge": add(100, -50.0), "loud": add(101, -49.9),
           "attempt": add(102, -70.0, ev=WAKELIKE, auto="attempt", why="attempt"),
           "oye": add(103, -70.0, ev="ph=0;weak=0;kivi=0;oye=1;ksim=0.5"),
           "key_attempt": add(104, -70.0, auto="attempt", why="attempt"),
           "no_ev": add(105, -70.0, ev=""),
           "owner_noise": add(106, -70.0, "noise", "owner"), "owner_unclear": add(107, -70.0, "unclear", "owner"),
           "owner_attempt": add(108, -70.0, "attempt", "owner"),
           "auto_noise": add(109, -70.0, "noise", "auto-v1", auto="noise", why="")}
    L.write_miss_table(root / "live-misses" / Q.TABLE, rows)
    L.write_csv_atomic(root / "labelq" / "plan.csv", Q.PLAN_FIELDS, [dict(p, order=str(n)) for n, p in enumerate(plan, 1)])
    L.write_csv_atomic(root / "labelq" / "key.csv", Q.KEY_FIELDS, key)
    return root, ids


def _snap(root):
    return {p.name: p.read_bytes() for d in ("live-misses", "labelq") for p in (root / d).glob("*.csv")}


def _qns(root, dry=False):
    return type("A", (), {"dataset_root": str(root), "dry_run": dry})()


def _audited(root):
    with open(root / "labelq" / "key.csv", newline="") as f:
        return sorted(k["ref"] for k in csv.DictReader(f) if k["audit"] == "1")


def test_quiet_noise_command(tmp_path, capsys):
    root, ids = _quiet_root(tmp_path)
    before = _snap(root)
    Q.cmd_quiet_noise(_qns(root, dry=True))
    out = capsys.readouterr().out
    assert _snap(root) == before and not list(root.rglob("*.bak-*"))             # --dry-run writes nothing
    assert "total: relabelled 41, audited 3" in out                              # 5 % of 41 rounded up
    assert "train: relabelled 27" in out and "test: relabelled 14" in out
    assert "skipped 4" in out and "already labelled 4" in out
    Q.cmd_quiet_noise(_qns(root))
    rows = {r["id"]: r for r in L.read_miss_table(root / "live-misses" / Q.TABLE)}
    quiet = [r for r in rows.values() if r["label_source"] == Q.QUIET_SOURCE]
    assert len(quiet) == 41 and all(r["label"] == "noise" and r["label_conf"] == "0.90" and "quiet=1" in r["auto_evidence"]
                                    for r in quiet)
    for k in ("edge", "loud", "attempt", "oye", "key_attempt", "no_ev"):          # to the owner, untouched
        assert rows[ids[k]]["label"] == "" and rows[ids[k]]["label_source"] == ""
    assert [rows[ids[k]]["label"] for k in ("owner_noise", "owner_unclear", "owner_attempt")] == ["noise", "unclear", "attempt"]
    assert all(rows[ids[k]]["label_source"] == "owner" and rows[ids[k]]["speaker"] == "spk01"
               for k in ("owner_noise", "owner_unclear", "owner_attempt"))
    assert rows[ids["auto_noise"]]["label_source"] == "auto-v1"
    with open(root / "labelq" / "key.csv", newline="") as f:
        key = {k["ref"]: k for k in csv.DictReader(f)}
    audited = set(_audited(root))
    assert len(audited) == 3 and audited <= {r["id"] for r in quiet}
    for r in quiet:
        k = key[r["id"]]
        assert (k["auto_label"], k["auto_conf"]) == ("noise", "0.90")
        assert k["why"] == ("audit" if r["id"] in audited else "quiet")
    assert key[ids["attempt"]]["auto_label"] == "attempt" and key[ids["edge"]]["why"] == "unclear"
    with open(root / "labelq" / "plan.csv", newline="") as f:
        plan = list(csv.DictReader(f))
    left = {ids[k] for k in ("edge", "loud", "attempt", "oye", "key_attempt", "no_ev", "owner_noise", "owner_unclear",
                             "owner_attempt", "auto_noise")}
    assert [p["order"] for p in plan] == [str(n) for n in range(1, len(plan) + 1)]
    assert {p["ref"] for p in plan} == audited | left
    asked = {it["row"]["id"] for it in L.queue_items(root)}                      # trusted rows are not asked, owner ones neither
    assert asked == audited | (left - {ids[k] for k in ("owner_noise", "owner_unclear", "owner_attempt")})
    # idempotent: a second run changes nothing and writes no backup
    after = _snap(root)
    nbak = len(list(root.rglob("*.bak-*")))
    Q.cmd_quiet_noise(_qns(root))
    assert "nothing to do" in capsys.readouterr().out
    assert _snap(root) == after and len(list(root.rglob("*.bak-*"))) == nbak
    # the report and the audit command count the new rows
    ns = type("A", (), {"dataset_root": str(root), "json": None})()
    Q.cmd_report(ns)
    assert "auto-quiet" in capsys.readouterr().out
    Q.cmd_audit(ns)
    assert "not labelled yet" in capsys.readouterr().out


def test_quiet_noise_audit_is_deterministic_and_owner_wins(tmp_path):
    a, _ = _quiet_root(tmp_path / "a")
    b, _ = _quiet_root(tmp_path / "b")
    Q.cmd_quiet_noise(_qns(a))
    Q.cmd_quiet_noise(_qns(b))
    assert _audited(a) == _audited(b) and len(_audited(a)) == 3
    # the owner labels an audited row; a later run keeps the owner label and the audit mark
    aud = _audited(a)[0]
    L.save_miss_label(a / "live-misses" / Q.TABLE, aud, "attempt", "spk02")
    Q.cmd_quiet_noise(_qns(a))
    row = next(r for r in L.read_miss_table(a / "live-misses" / Q.TABLE) if r["id"] == aud)
    assert (row["label"], row["label_source"]) == ("attempt", "owner") and aud in _audited(a)
    # a pending quiet row that the owner labelled before the run keeps the owner label
    c, _ = _quiet_root(tmp_path / "c")
    first = next(r["id"] for r in L.read_miss_table(c / "live-misses" / Q.TABLE) if r["level_dbfs"] == "-55.0")
    L.save_miss_label(c / "live-misses" / Q.TABLE, first, "attempt", "spk02")
    Q.cmd_quiet_noise(_qns(c))
    row = next(r for r in L.read_miss_table(c / "live-misses" / Q.TABLE) if r["id"] == first)
    assert (row["label"], row["label_source"]) == ("attempt", "owner")
