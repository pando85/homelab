"""Light tests of wake_release.py: no audio, no network, no cluster, no Home Assistant.

    cd scripts/voice-assistant/wakeword && $KIVI_HOME/.venv/bin/python -m pytest test_wake_release.py -q
"""
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import wake_release as W  # noqa: E402

V = "v5-oye-kivi-2026-11-20"
SHA_A = "a" * 64
SHA_B = "b" * 64


@pytest.fixture
def releases(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "RELEASES", tmp_path / "releases")
    monkeypatch.setattr(W, "PLAN", False)
    return tmp_path / "releases"


def make_state(releases, tmp_path, **steps):
    cand = tmp_path / "cand"
    cand.mkdir(exist_ok=True)
    assert W.main(["init", "--version", V, "--model-id", "oye_kivi", "--phrase", "oye Kivi", "--candidate", str(cand)]) == 0
    st = W.load_state(V)
    for k, v in steps.items():
        W.record(st, k, **v)
    return st


# ------------------------------------------------------------------------------------- names, lock

def test_version_and_id_rules():
    for ok in ["v1-2026-11-05", "v4-oye-kivi-2026-11-07", "v12-ey-kivi-2027-01-31"]:
        W.check_version(ok)
    for bad in ["v4", "4-oye-2026-11-07", "v4-Oye-2026-11-07", "v4-oye-kivi-26-11-07", "v4_oye_2026-11-07", ""]:
        with pytest.raises(W.Refused):
            W.check_version(bad)
    assert W.ID_RE.match("oye_kivi") and not W.ID_RE.match("Oye-Kivi")


def test_init_idempotent_and_guarded(releases, tmp_path):
    make_state(releases, tmp_path)
    d = releases / V
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    before = (d / "state.json").read_text()
    args = ["init", "--version", V, "--model-id", "oye_kivi", "--phrase", "oye Kivi", "--candidate", str(tmp_path / "cand")]
    assert W.main(args) == 0 and (d / "state.json").read_text() == before            # same config: no-op
    assert W.main(args[:-1] + [str(tmp_path / "cand"), "--pipeline-name", "other"]) == 2  # changed: refused
    assert W.main(["init", "--version", V, "--model-id", "kivi_v2", "--phrase", "x", "--candidate", str(tmp_path / "cand"),
                   "--update"]) == 2                                                  # stem would lose _v2


def test_lock_read_and_update(tmp_path):
    p = tmp_path / "models.lock"
    p.write_text(f"# comment\nkivi      v1-2026-11-05           {SHA_A}\n\n")
    assert W.read_lock(p) == {"kivi": ("v1-2026-11-05", SHA_A)}
    t = W.update_lock_text(p.read_text(), "oye_kivi", "v4-oye-kivi-2026-11-07", SHA_B)
    assert t.splitlines()[0] == "# comment" and t.rstrip().endswith(SHA_B)
    t2 = W.update_lock_text(t, "oye_kivi", V, SHA_A)                                  # replaces, keeps one line
    p.write_text(t2)
    assert W.read_lock(p) == {"kivi": ("v1-2026-11-05", SHA_A), "oye_kivi": (V, SHA_A)}
    p.write_text(t2 + f"kivi v1-2026-11-05 {SHA_A}\n")
    with pytest.raises(W.Refused):
        W.read_lock(p)
    p.write_text("kivi v1 abc\n")
    with pytest.raises(W.Refused):
        W.read_lock(p)


VALUES = """      initContainers:
        01-copy-wake-words:
          image:
            # No renovate hint
            repository: git.grigri.cloud/agil/kivi-models
            tag: v4-oye-kivi-2026-11-07@sha256:%s
          command:
            - sh
            - -ec
            - cd /models && sha256sum -c SHA256SUMS && cp /models/*.tflite /custom/ && cd /custom && sha256sum *.tflite
""" % ("1" * 64)


def test_values_pin_bump():
    assert W.current_pin(VALUES).startswith("v4-oye-kivi-2026-11-07@sha256:111")
    new_text, new = W.bump_pin(VALUES, V, "sha256:" + "2" * 64)
    assert new == f"{V}@sha256:" + "2" * 64 and W.current_pin(new_text) == new
    assert new_text.count("\n") == VALUES.count("\n")
    with pytest.raises(W.Refused):
        W.bump_pin(VALUES, V, "sha256:xyz")
    with pytest.raises(W.Refused):                                                    # old single-model command
        W.bump_pin(VALUES.replace("cp /models/*.tflite /custom/", "cp /models/kivi.tflite /custom/kivi.tflite"), V,
                   "sha256:" + "2" * 64)


# ---------------------------------------------------------------------------------------- statistics

def test_intervals():
    lo, hi = W.poisson_ci(8)
    assert abs(lo / 8.311 - 0.416) < 0.002 and abs(hi / 8.311 - 1.897) < 0.002       # v4 room test part
    assert W.poisson_ci(0)[0] == 0 and abs(W.poisson_ci(0)[1] - 3.689) < 0.002
    assert W.wilson(5, 21) == [0.1063, 0.4509]
    assert W.wilson(0, 0) == [0.0, 1.0]


def summary(rows, room_k=1, hours=8.0):
    def cell(k, h):
        return {"hours": h, "k": k, "per_hour": round(k / h, 3),
                "ci95": [round(x / h, 3) for x in W.poisson_ci(k)]}
    half = hours / 2
    return {"threshold": 0.95, "recall": rows,
            "room_test": {"pooled": cell(room_k, hours),
                          "c3-a": cell(room_k // 2, half),
                          "c3-b": cell(room_k - room_k // 2, half)}}


def row(spk, cond, k, n, split="test", eval_channel=True):
    return {"session": "s", "source": "tv-c3a" if eval_channel else "pc-mic", "speaker": spk, "split": split,
            "eval_channel": eval_channel, "condition": cond, "k": k, "n": n}


def test_gate_pass_fail_incomplete():
    ok = [row("spk01", "tv-off", 18, 20), row("spk01", "tv-on", 15, 20), row("spk02", "tv-off", 9, 10),
          row("spk02", "tv-on", 7, 10), row("spk01", "tv-off", 0, 20, split="train"), row("spk01", "tv-on", 0, 20, eval_channel=False)]
    g = W.gate_eval(summary(ok))
    assert g["verdict"] == "pass" and not g["confident"]                              # point estimates only
    pooled = next(c for c in g["checks"] if c["check"] == "recall tv-off, pooled")
    assert (pooled["k"], pooled["n"]) == (27, 30)                                     # train and pc rows left out
    assert W.gate_eval(summary(ok, room_k=4, hours=8.0))["verdict"] == "fail"         # 0.5/h
    bad = ok[:3] + [row("spk02", "tv-on", 3, 10)]
    g = W.gate_eval(summary(bad))
    assert g["verdict"] == "fail" and any(c["check"] == "recall tv-on, spk02" and not c["pass"] for c in g["checks"])
    g = W.gate_eval({"threshold": 0.95, "recall": [], "room_test": None})
    assert g["verdict"] == "incomplete" and len(g["missing"]) >= 3
    assert "MISSING" in W.gate_text(g)


def test_other_devices_are_reported_apart_and_never_gated():
    tv = [row("spk01", "tv-off", 18, 20), row("spk01", "tv-on", 15, 20),
          row("spk02", "tv-off", 10, 10), row("spk02", "tv-on", 8, 10)]
    # PC rows of a test session (new rows say eval True, old rows said False: both count as evaluation, informational)
    pc = [{**row("spk01", "tv-off", 3, 36, eval_channel=False), "device_class": "pc-desk"},
          {**row("spk01", "tv-off", 0, 36, eval_channel=True), "source": "webcam-mic", "device_class": "pc-webcam"},
          {**row("spk01", "tv-on", 1, 20, eval_channel=False), "split": "train-only"}]
    base = W.gate_eval(summary(tv))
    g = W.gate_eval(summary(tv + pc))
    assert g["verdict"] == base["verdict"] == "pass" and g["checks"] == base["checks"]    # the TV verdict is unchanged
    assert [(o["device_class"], o["source"], o["k"], o["n"]) for o in g["other_devices"]] == [
        ("pc-desk", "pc-mic", 3, 36), ("pc-webcam", "webcam-mic", 0, 36)]               # the train-only row is left out
    assert g["other_devices"][0]["ci95"] == W.wilson(3, 36)
    txt = W.gate_text(g)
    assert "informational" in txt and "pc-desk" in txt and "satellite rows only" in txt
    assert W.gate_eval(summary(pc))["other_devices"] and W.gate_eval(summary(pc))["verdict"] == "incomplete"
    # an old summary without device_class: derived from the source label
    old = {k: v for k, v in pc[0].items() if k != "device_class"}
    assert W.row_class(old) == "pc-desk" and W.row_is_eval(old) and not W.row_is_eval({**old, "split": "train-only"})
    assert W.row_class({"source": "tv-c3b"}) == "tv" and W.row_class({"source": "phone"}) == "phone"


def test_gate_does_not_hide_missing_speaker_or_condition():
    complete = [row("spk01", "tv-off", 18, 20), row("spk01", "tv-on", 15, 20),
                row("spk02", "tv-off", 10, 10), row("spk02", "tv-on", 8, 10)]
    one_speaker = W.gate_eval(summary(complete[:2]))
    assert one_speaker["verdict"] == "incomplete"
    assert any("2 distinct test speakers" in x for x in one_speaker["missing"])

    missing_condition = W.gate_eval(summary(complete[:-1]))
    assert missing_condition["verdict"] == "incomplete"
    assert any("spk02: tv-on" in x for x in missing_condition["missing"])

    few_takes = W.gate_eval(summary(complete[:3] + [row("spk02", "tv-on", 4, 4)]))
    assert few_takes["verdict"] == "incomplete"
    assert any("need >= 10" in x for x in few_takes["missing"])

    unassigned = W.gate_eval(summary(complete + [row(None, "tv-off", 1, 1)]))
    assert unassigned["verdict"] == "incomplete"


def test_gate_checks_room_per_device_and_requires_both():
    complete = [row("spk01", "tv-off", 18, 20), row("spk01", "tv-on", 15, 20),
                row("spk02", "tv-off", 10, 10), row("spk02", "tv-on", 8, 10)]
    mixed = summary(complete, room_k=2, hours=8)
    # Pooled 0.25/h appears acceptable, but both wakes occur on c3-b: 0.5/h.
    mixed["room_test"]["c3-a"] = {"hours": 4, "k": 0, "per_hour": 0, "ci95": [0, 0.9]}
    mixed["room_test"]["c3-b"] = {"hours": 4, "k": 2, "per_hour": 0.5, "ci95": [0.06, 1.8]}
    assert W.gate_eval(mixed)["verdict"] == "fail"
    del mixed["room_test"]["c3-b"]
    g = W.gate_eval(mixed)
    assert g["verdict"] == "incomplete"
    assert any("2 distinct devices" in x for x in g["missing"])


# ----------------------------------------------------------------------------------- score summaries

def test_summaries_from_tool_json():
    hits = {"0.5": {"k": 9, "n": 10}, "0.9437": {"k": 7, "n": 10}}
    sess = [{"session": "S1", "speaker": "spk01", "split": "test", "source": "tv-c3a",
             "overall": {"kivi_by_condition": {"1.5m_tv-off": {"cue_hits": {W.NAME: hits}},
                                               "3m_tv-on": {"cue_hits": {W.NAME: hits}},
                                               "ALL": {"cue_hits": {W.NAME: hits}}}}}]
    info = {"tv-c3a": {"speaker": "spk01", "split": "test", "eval_channel": True}}
    rows = W.summarize_session(sess, 0.9437, info)
    assert [(r["condition"], r["k"], r["n"]) for r in rows] == [("tv-off", 7, 10), ("tv-on", 7, 10)]
    with pytest.raises(W.Refused):
        W.summarize_session(sess, 0.98, info)                                         # threshold not scored
    btn = {"session": "button-x", "recall": {W.NAME: {"0.9437": {"all_takes": {"k": 3, "n": 15}}}}}
    r = W.summarize_button(btn, 0.9437, {"tv-c3b": {"speaker": "spk02", "split": "test", "eval_channel": True}}, "tv-on")
    assert r[0]["condition"] == "tv-on" and (r[0]["k"], r[0]["n"]) == (3, 15) and r[0]["speaker"] == "spk02"
    room = {"scopes": {"pooled": {"hours": 8.311, "false_accepts": {W.NAME: {"0.9437": {"k": 8}}}}}}
    s = W.summarize_room(room, 0.9437)["pooled"]
    assert s["per_hour"] == 0.963 and s["ci95"] == [0.416, 1.897]
    assert W.condition_of("kivi-3m-tvoff") == "tv-off" and W.condition_of("1.5m_tv-on") == "tv-on"


# ------------------------------------------------------------------------------------- HA helpers

def test_trial_pipeline_and_entities():
    base = {"id": "x", "name": "Kiwi", "conversation_engine": "conversation.fast", "language": "es",
            "wake_word_entity": "wake_word.openwakeword", "wake_word_id": "alexa", "prefer_local_intents": True}
    f = W.trial_pipeline_fields(base, "oye Kivi", "oye_kivi")
    assert f["name"] == "oye Kivi" and f["wake_word_id"] == "oye_kivi" and "id" not in f
    assert {k: v for k, v in f.items() if k not in ("name", "wake_word_id")} == \
           {k: v for k, v in base.items() if k not in ("id", "name", "wake_word_id")}
    plist = {"preferred_pipeline": "x", "pipelines": [base, dict(f, id="y")]}
    assert W.resolve_option(plist, "preferred")["name"] == "Kiwi" and W.resolve_option(plist, "oye Kivi")["id"] == "y"
    reg = [{"entity_id": "select.tv_a_assistant", "platform": "esphome", "device_id": "bd8256e9", "unique_id": "m-pipeline"},
           {"entity_id": "select.tv_a_assistant_2", "platform": "esphome", "device_id": "bd8256e9", "unique_id": "m-pipeline_2"},
           {"entity_id": "binary_sensor.tv_a_microphone_muted", "platform": "esphome", "device_id": "bd8256e9", "unique_id": "q"},
           {"entity_id": "select.tv_b_assistant", "platform": "esphome", "device_id": "fda18f73", "unique_id": "n-pipeline"}]
    e = W.tv_entities(reg, "c3-a", {"c3-a": "bd8256", "c3-b": "fda18f"})
    assert e == {"select": "select.tv_a_assistant", "muted": "binary_sensor.tv_a_microphone_muted", "device_id": "bd8256e9",
                 "satellite": None}
    with pytest.raises(W.Refused):
        W.tv_entities(reg, "c3-z", {"c3-a": "bd8256"})


# ---------------------------------------------------------------------------------------- guards

def test_owner_gates_refuse_before_any_network(releases, tmp_path, monkeypatch):
    make_state(releases, tmp_path)

    def boom(*a, **k):
        raise AssertionError("network or git touched")
    monkeypatch.setattr(W, "git", boom)
    monkeypatch.setattr(W.HA, "__enter__", boom)
    assert W.main(["release", "--version", V]) == 2
    assert W.main(["enable", "--version", V, "--tv", "c3-a"]) == 2
    # approved, but the gate did not pass and no override: still refused before git
    assert W.main(["release", "--version", V, "--yes-owner-approved"]) == 2


def test_pick_threshold_never_uses_the_test_part(releases, tmp_path):
    make_state(releases, tmp_path)
    test_part = tmp_path / "room_c3-a_test.npz"
    test_part.write_bytes(b"")
    assert W.main(["pick-threshold", "--version", V, "--room-sel", str(test_part)]) == 2
    assert W.main(["pick-threshold", "--version", V, "--threshold", "0.98"]) == 2      # owner value needs a reason
    assert W.main(["pick-threshold", "--version", V, "--threshold", "0.98", "--reason", "owner, test"]) == 0
    assert W.load_state(V)["steps"]["pick-threshold"]["source"] == "owner"


def test_adopt_session_records_a_release_that_reuses_old_recordings(releases, tmp_path, monkeypatch):
    make_state(releases, tmp_path)
    ds = tmp_path / "dataset"
    (ds / "S1").mkdir(parents=True)
    (ds / "manifest.jsonl").write_text(
        json.dumps({"session": "S1", "kind": "take", "phrase": "oye kivi", "source": "tv-c3a",
                    "speaker": "spk01", "split": "train"}) + "\n" +
        json.dumps({"session": "S2", "kind": "take", "phrase": "kivi", "source": "tv-c3a"}) + "\n")
    (ds / "S2").mkdir()
    monkeypatch.setattr(W, "DATASET", ds)
    monkeypatch.setattr(W, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-extracted")))
    assert W.main(["extract", "--version", V, "--adopt-session", "nope"]) == 2               # not a session dir
    assert W.main(["extract", "--version", V, "--adopt-session", "S2"]) == 2                 # another phrase
    assert W.main(["extract", "--version", V, "--adopt-session", "S1"]) == 0
    assert W.load_state(V)["sessions"] == [{"id": "S1", "kind": "session", "condition": None}]
    assert W.main(["extract", "--version", V, "--adopt-session", "S1"]) == 0                 # idempotent
    assert len(W.load_state(V)["sessions"]) == 1


def test_plan_writes_nothing(releases, tmp_path):
    cand = tmp_path / "cand"
    cand.mkdir()
    assert W.main(["--plan", "init", "--version", V, "--model-id", "oye_kivi", "--phrase", "oye Kivi",
                   "--candidate", str(cand)]) == 0
    assert not (releases / V / "state.json").exists()


def test_model_card_render(releases, tmp_path):
    st = make_state(releases, tmp_path, **{"pick-threshold": {"threshold": 0.95, "source": "selection", "max_fa": 2,
                                                               "sel_fa": 2, "sel_hours": 8.4}})
    md = tmp_path / "model"
    md.mkdir()
    (md / "oye_kivi.tflite").write_bytes(b"x" * 10)
    (releases / V / "score_summary.json").write_text(json.dumps(summary([row("spk01", "tv-off", 8, 10)])))
    v = W.card_values(st, md)
    out = W.render_card("{model_id} {threshold} {real_recall} {room_fa} {unknown_field} {gate_verdict}", v)
    assert "oye_kivi 0.95" in out and "| spk01 |" in out and "not measured" in out and "not run" in out


@pytest.mark.skipif(not (W.KIVI_MODELS / "image/stage-models.sh").exists() or not shutil.which("sha256sum"),
                    reason="needs the private kivi-models checkout")
def test_image_staging_script_checks_pins(tmp_path):
    src = tmp_path / "wakeword/m_one/v1-x-2026-01-01"
    src.mkdir(parents=True)
    (src / "m_one.tflite").write_bytes(b"model")
    good = subprocess.run(["sha256sum", str(src / "m_one.tflite")], capture_output=True, text=True).stdout.split()[0]
    script = str(W.KIVI_MODELS / "image/stage-models.sh")
    for sha, rc in [(good, 0), ("0" * 64, 1)]:
        (tmp_path / "lock").write_text(f"# c\nm_one v1-x-2026-01-01 {sha}\n")
        out = tmp_path / f"out-{rc}"
        r = subprocess.run(["sh", script, str(tmp_path / "lock"), str(tmp_path / "wakeword"), str(out)],
                           capture_output=True, text=True)
        assert (r.returncode != 0) == bool(rc), r.stdout + r.stderr
    assert (tmp_path / "out-0/m_one.tflite").exists() and "m_one.tflite" in (tmp_path / "out-0/SHA256SUMS").read_text()
    os.chmod(tmp_path / "out-0", 0o755)


def test_promote_fields_and_moves():
    kiwi = {"id": "k", "name": "Kiwi", "wake_word_entity": "wake_word.openwakeword", "wake_word_id": "alexa",
            "conversation_engine": "conversation.fast", "prefer_local_intents": True}
    trial = dict(kiwi, id="t", name="oye Kivi", wake_word_id="oye_kivi")
    f = W.promoted_fields(kiwi, "oye_kivi")
    assert "id" not in f and f["wake_word_id"] == "oye_kivi"
    assert {k: v for k, v in f.items() if k != "wake_word_id"} == {k: v for k, v in kiwi.items() if k not in ("id", "wake_word_id")}
    plist = {"preferred_pipeline": "k", "pipelines": [kiwi, trial]}
    sel = {"c3-a": "oye Kivi", "c3-b": "preferred", "c3-c": None}
    assert W.tvs_on_pipeline(sel, plist, "oye Kivi") == ["c3-a"]
    assert W.tvs_on_pipeline(sel, plist, "Kiwi") == ["c3-b"]


def test_promote_needs_owner_and_demote_does_not(releases, tmp_path, monkeypatch):
    make_state(releases, tmp_path)
    entered = []

    def fake_enter(self):
        entered.append(1)
        raise W.Refused("no network in tests")
    monkeypatch.setattr(W.HA, "__enter__", fake_enter)
    assert W.main(["promote", "--version", V]) == 2 and not entered      # refused before any HA contact
    assert W.main(["demote", "--version", V]) == 2 and entered == [1]    # rollback goes straight to HA


class FakeHA:
    """Just enough HA for promote: Kiwi already on the model (changed outside the driver), no trial pipeline."""
    updates = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *e):
        pass

    def wake_words(self, entity="x"):
        return ["alexa", "oye_kivi"]

    def pipelines(self):
        kiwi = {"id": "k", "name": "Kiwi", "wake_word_entity": "wake_word.openwakeword", "wake_word_id": "oye_kivi"}
        return {"preferred_pipeline": "k", "pipelines": [kiwi]}

    def states(self):
        return {}

    def call(self, t, **kw):
        if t == "config/entity_registry/list":
            return []
        FakeHA.updates.append(t)
        return {}


def test_promote_records_a_change_made_outside_the_driver(releases, tmp_path, monkeypatch):
    make_state(releases, tmp_path)
    st = W.load_state(V)
    st["tvs"] = {"c3-a": {"previous": "preferred"}}                      # stale: the trial pipeline is gone
    W.save_state(st)
    monkeypatch.setattr(W, "HA", FakeHA)
    monkeypatch.setattr(W, "devices_map", lambda: {})
    assert W.main(["promote", "--version", V, "--yes-owner-approved", "--previous-wake-word", "alexa",
                   "--owner-override", "gate incomplete, owner decision"]) == 0
    assert FakeHA.updates == []                                          # nothing written to Home Assistant
    st = W.load_state(V)
    assert st["promotion"]["previous_wake_word"] == "alexa" and st["promotion"]["recorded_after_the_fact"]
    assert "tvs" not in st
    assert st["log"][-1]["step"] == "promote"
    assert W.main(["promote", "--version", V, "--yes-owner-approved", "--previous-wake-word", "oye_kivi"]) == 2  # the model itself


# ----------------------------------------------------------------------- run check and pipeline preflight

SAT_A = "assist_satellite.lg_c3_c3_a_voice_satellite_assist_satellite"
SAT_B = "assist_satellite.lg_c3_c3_b_voice_satellite_assist_satellite"
T0 = W.dt.datetime(2026, 11, 11, 18, 0, 0, tzinfo=W.dt.timezone.utc)


def run(rid, ts, sat, *types, code=None):
    ev = [{"type": "run-start", "data": {"pipeline": "p", "satellite_id": sat}}]
    ev += [{"type": t, "data": {"code": code} if t == "error" else None} for t in types]
    return {"pipeline_run_id": rid, "timestamp": ts, "events": ev}


class RunsHA:
    def __init__(self, runs):
        self._runs = runs

    def runs(self, pipeline_id):
        return [{k: r[k] for k in ("pipeline_run_id", "timestamp")} for r in self._runs]

    def run_events(self, pipeline_id, run_id):
        return next(r["events"] for r in self._runs if r["pipeline_run_id"] == run_id)


@pytest.fixture
def fast_clock(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(W.time, "time", lambda: t[0])
    monkeypatch.setattr(W.time, "sleep", lambda s: t.__setitem__(0, t[0] + s))


def test_check_run_ok_run(fast_clock):
    ha = RunsHA([run("1", "2026-11-11T18:00:05+00:00", SAT_A, "wake_word-start")])
    assert W.check_run(ha, "p", SAT_A, T0) == (True, "ok")


def test_check_run_errored_run_reports_the_code(fast_clock):
    ha = RunsHA([run("1", "2026-11-11T18:00:05+00:00", SAT_A, "error", "run-end", code="intent-not-supported")])
    assert W.check_run(ha, "p", SAT_A, T0, timeout_s=6) == (False, "intent-not-supported")


def test_check_run_error_after_wake_word_start_is_not_ok(fast_clock):
    ha = RunsHA([run("1", "2026-11-11T18:00:05+00:00", SAT_A, "wake_word-start", "error", code="wake-provider-timeout")])
    assert W.check_run(ha, "p", SAT_A, T0, timeout_s=4) == (False, "wake-provider-timeout")


def test_check_run_ignores_other_satellites_and_old_runs(fast_clock):
    ha = RunsHA([run("1", "2026-11-11T18:00:05+00:00", SAT_B, "wake_word-start"),
                 run("2", "2026-11-11T17:59:00+00:00", SAT_A, "wake_word-start")])
    ok, detail = W.check_run(ha, "p", SAT_A, T0, timeout_s=4)
    assert not ok and "no run of" in detail


def test_check_run_only_the_newest_run_of_the_satellite_counts(fast_clock):
    ha = RunsHA([run("1", "2026-11-11T18:00:05+00:00", SAT_A, "wake_word-start"),
                 run("2", "2026-11-11T18:00:20+00:00", SAT_A, "error", code="stt-provider-unsupported-language")])
    assert W.check_run(ha, "p", SAT_A, T0, timeout_s=2) == (False, "stt-provider-unsupported-language")


def test_check_run_waits_for_the_run(fast_clock):
    class Late(RunsHA):
        n = 0

        def runs(self, pid):
            Late.n += 1
            return super().runs(pid) if Late.n > 3 else []
    ha = Late([run("1", "2026-11-11T18:00:05+00:00", SAT_A, "wake_word-start")])
    assert W.check_run(ha, "p", SAT_A, T0) == (True, "ok")


PIPE = {"name": "Kiwi Test", "conversation_engine": "conversation.fast", "stt_engine": "stt.faster_whisper",
        "tts_engine": "tts.piper", "wake_word_id": "alexa"}
STATES = {"conversation.fast": {"state": "2026-11-11T00:00:00"}, "stt.faster_whisper": {"state": "unknown"},
          "tts.piper": {"state": "unknown"}}
SERVED = ["alexa", "oye_kivi_next"]


def test_preflight_passes_a_healthy_pipeline():
    assert W.preflight_pipeline(PIPE, STATES, SERVED) == []


def test_preflight_refuses_an_unavailable_engine_unless_forced(capsys):
    states = dict(STATES, **{"conversation.fast": {"state": "unavailable"}})
    with pytest.raises(W.Refused, match="conversation_engine conversation.fast is unavailable"):
        W.preflight_pipeline(PIPE, states, SERVED)
    assert W.preflight_pipeline(PIPE, states, SERVED, force=True)
    assert "--force given" in capsys.readouterr().out
    gone = {k: v for k, v in STATES.items() if k != "tts.piper"}
    with pytest.raises(W.Refused, match="tts.piper does not exist"):
        W.preflight_pipeline(PIPE, gone, SERVED)


def test_preflight_refuses_an_unserved_wake_word():
    with pytest.raises(W.Refused, match="alexa_v0.1' is not served"):
        W.preflight_pipeline(dict(PIPE, wake_word_id="alexa_v0.1"), STATES, SERVED)


def test_preflight_ignores_legacy_engine_ids_and_no_wake_word():
    assert W.preflight_pipeline(dict(PIPE, conversation_engine="homeassistant", wake_word_id=None), STATES, SERVED) == []


class RollbackHA(RunsHA):
    """Home Assistant for `rollback --to Kiwi Test`: c3-a on the preferred pipeline."""
    calls = []

    def __init__(self, runs, states=STATES, served=SERVED):
        super().__init__(runs)
        self.st = dict(states, **{"select.lg_c3_c3_a_voice_satellite_assistant": {"state": "preferred"}})
        self.served = served

    def __call__(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *e):
        pass

    def wake_words(self, entity="x"):
        return self.served

    def pipelines(self):
        return {"preferred_pipeline": "k", "pipelines": [
            {"id": "k", "name": "Kivi", "wake_word_id": "oye_kivi_next", "wake_word_entity": "w"},
            dict(PIPE, id="t", wake_word_entity="w")]}

    def states(self):
        return self.st

    def kick(self, p):
        RollbackHA.calls.append(("kick", p["id"]))

    def call(self, t, **kw):
        if t == "config/entity_registry/list":
            return [{"entity_id": "select.lg_c3_c3_a_voice_satellite_assistant", "platform": "esphome", "device_id": "bd8256x",
                     "unique_id": "m-pipeline"},
                    {"entity_id": SAT_A, "platform": "esphome", "device_id": "bd8256x", "unique_id": "m-sat"}]
        RollbackHA.calls.append((t, kw))
        return {}


def run_rollback(monkeypatch, releases, tmp_path, ha, capsys):
    make_state(releases, tmp_path)
    RollbackHA.calls = []
    monkeypatch.setattr(W, "HA", ha)
    monkeypatch.setattr(W, "devices_map", lambda: {"c3-a": "bd8256"})
    code = W.main(["rollback", "--version", V, "--tv", "c3-a", "--to", "Kiwi Test"])
    return code, capsys.readouterr()


def test_rollback_ok_run_passes(releases, tmp_path, monkeypatch, capsys, fast_clock):
    ts = (W.dt.datetime.now(W.dt.timezone.utc) + W.dt.timedelta(hours=1)).isoformat()
    ha = RollbackHA([run("1", ts, SAT_A, "wake_word-start")])
    code, out = run_rollback(monkeypatch, releases, tmp_path, ha, capsys)
    assert code == 0 and ": OK" in out.out and "FAILED" not in out.out
    assert any(c[0] == "call_service" for c in RollbackHA.calls)


def test_rollback_errored_run_fails_and_prints_the_way_back(releases, tmp_path, monkeypatch, capsys, fast_clock):
    ts = (W.dt.datetime.now(W.dt.timezone.utc) + W.dt.timedelta(hours=1)).isoformat()
    ha = RollbackHA([run("1", ts, SAT_A, "error", "run-end", code="intent-not-supported")])
    code, out = run_rollback(monkeypatch, releases, tmp_path, ha, capsys)
    assert code == 2 and "FAILED: intent-not-supported" in out.out
    assert "rollback --version" in out.out and "--to preferred --force" in out.out
    assert [c for c in RollbackHA.calls if c[0] == "call_service"][0][1]["service_data"] == {"option": "Kiwi Test"}  # select left


def test_rollback_other_satellites_run_does_not_count(releases, tmp_path, monkeypatch, capsys, fast_clock):
    ts = (W.dt.datetime.now(W.dt.timezone.utc) + W.dt.timedelta(hours=1)).isoformat()
    ha = RollbackHA([run("1", ts, SAT_B, "wake_word-start")])
    code, out = run_rollback(monkeypatch, releases, tmp_path, ha, capsys)
    assert code == 2 and "FAILED: no run of" in out.out


def test_rollback_refuses_an_unavailable_engine_before_switching(releases, tmp_path, monkeypatch, capsys, fast_clock):
    ha = RollbackHA([], states=dict(STATES, **{"conversation.fast": {"state": "unavailable"}}))
    code, out = run_rollback(monkeypatch, releases, tmp_path, ha, capsys)
    assert code == 2 and "unavailable" in out.err and "--force" in out.err
    assert not RollbackHA.calls


def test_rollback_refuses_an_unserved_wake_id_before_switching(releases, tmp_path, monkeypatch, capsys, fast_clock):
    ha = RollbackHA([], served=["oye_kivi_next"])
    code, out = run_rollback(monkeypatch, releases, tmp_path, ha, capsys)
    assert code == 2 and "'alexa' is not served" in out.err and not RollbackHA.calls


def test_household_pipeline_is_found_by_the_preferred_flag_not_its_name():
    for household in ("Kiwi", "Kivi"):
        plist = {"preferred_pipeline": "k", "pipelines": [{"id": "k", "name": household}, {"id": "t", "name": "Kiwi Test"}]}
        assert W.resolve_option(plist, "preferred")["id"] == "k"
        assert W.tvs_on_pipeline({"c3-a": "preferred", "c3-b": "Kiwi Test"}, plist, household) == ["c3-a"]
