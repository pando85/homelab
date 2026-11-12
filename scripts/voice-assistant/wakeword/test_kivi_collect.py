"""Tests of kivi_collect.py: pending days, day parity, heavy-job counting, idempotent tables, the lock, the disk guard and the
whole run with a fake step runner. No audio, no cluster, no model.

    cd scripts/voice-assistant/wakeword && OMP_NUM_THREADS=4 \
        $KIVI_HOME/.venv/bin/python -m pytest test_kivi_collect.py -q
"""
import json
import os
import sys
from argparse import Namespace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "4")

import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kivi_collect as Q  # noqa: E402
import kivi_live_wakes as L  # noqa: E402

PS = """  100  1 /usr/bin/zsh -c run something
 3327578  1 .venv/bin/python -I /h/wakeword/v6_replay.py --src negatives:* --model v4=x
 4011073  1 bash /h/wakeword/v6p2_compare.sh
 4011096 4011073 .venv/bin/python -I /h/wakeword/v6_replay.py --src a --model v5=y
 4011200 4011096 /usr/bin/python -c from multiprocessing.spawn import spawn_main
  555 100 python3 kivi_collect.py run
  556 555 grep v6_replay
"""


def test_heavy_jobs_count_process_trees_once_and_skip_ourselves():
    assert Q.heavy_jobs(PS.splitlines(), own_pid=555) == [3327578, 4011073]
    assert Q.heavy_jobs(PS.splitlines()[:1], own_pid=555) == []
    # a second collect (not an ancestor) is a heavy job too
    lines = PS.splitlines() + ["  900  1 python3 kivi_collect.py run"]
    assert 900 in Q.heavy_jobs(lines, own_pid=555)


def test_heavy_jobs_ignore_waiters_that_only_mention_a_job():
    lines = ["  10  1 timeout 178 bash -c until ! pgrep -f v6_replay.py; do sleep 20; done",
             "  11 10 bash -c until ! pgrep -f v6_replay.py; do sleep 20; done",
             "  12  1 /usr/bin/zsh -c eval 'python v6_replay.py'",
             "  13  1 pgrep -af v6_replay", "  14  1 tail -f /x/kivi_replay_mine.log", "  15  1 vim v6_replay.py",
             "  20  1 bash /h/v6p2_compare.sh", "  21 20 .venv/bin/python -I /h/v6_replay.py --src a",
             "  22  1 systemd-run --user --scope -- nice -n 10 python /h/kivi_replay_mine.py score"]
    assert Q.heavy_jobs(lines, own_pid=999) == [20, 22]


def test_day_role_is_parity_across_year_end():
    assert [Q.role_of(d) for d in ("2026-11-10", "2026-11-11", "2026-12-31", "2027-01-01", "2027-01-02")] == \
        ["mine", "test", "test", "test", "mine"]


def test_pending_days_need_a_start_skip_done_and_repeat_early():
    today = date(2026, 11, 14)
    assert Q.pending_days({"start": None, "days": {}}, today) == []
    st = {"start": "2026-11-10", "days": {"2026-11-10": {"done": True}, "2026-11-11": {"done": False},
                                         "2026-11-12": {"done": True, "early": True}}}
    assert Q.pending_days(st, today) == ["2026-11-11", "2026-11-12", "2026-11-13"]      # up to yesterday; today never
    assert Q.pending_days({"start": "2026-11-14", "days": {}}, today) == []


def test_live_wake_id_reads_only_the_household_pipeline():
    js = json.dumps({"data": {"items": [{"name": "Kiwi", "wake_word_id": "oye_kivi_next"},
                                        {"name": "Kiwi Test", "wake_word_id": "alexa"}]}})
    assert Q.live_wake_id(js) == "oye_kivi_next"
    with pytest.raises(SystemExit):
        Q.live_wake_id(json.dumps({"data": {"items": [{"name": "Other", "wake_word_id": "x"}]}}))


def row(i, utc, label="", split="", source=""):
    return {k: "" for k in L.FIELDS} | {"run_id": f"r{i}", "device": "c3-a", "utc": utc, "label": label, "split": split,
                                        "label_source": source, "trigger": "wake"}


def test_split_by_date_parity_only_true_false_and_only_that_day():
    rows = [row(1, "20261111T100000.000Z", "true"), row(2, "20261111T100000.000Z", "unclear"),
            row(3, "20261110T100000.000Z", "false"), row(4, "20261231T120000.000Z", "false"),
            row(5, "20261231T233000.000Z", "false")]                       # 23:30Z on 12-31 is already 01-01 in Madrid
    assert Q.assign_split(rows, "2026-11-11") == 1 and rows[0]["split"] == "test" and rows[1]["split"] == "" and rows[2]["split"] == ""
    assert Q.assign_split(rows, "2026-12-31") == 1 and rows[3]["split"] == "test"
    assert Q.assign_split(rows, "2027-01-01") == 1 and rows[4]["split"] == "test"
    assert Q.assign_split(rows, "2027-01-02") == 0


def window(i, utc, label=""):
    return {k: "" for k in L.MISS_FIELDS} | {"id": f"w{i}", "device": "c3-a", "utc": utc, "label": label, "clip": f"x/{i}.wav"}


def test_append_window_rows_is_idempotent_and_never_changes_existing_rows(tmp_path):
    t = tmp_path / "daily_false_wakes.csv"
    assert Q.append_window_rows(t, [window(1, "20261111T100000.000Z"), window(2, "20261111T110000.000Z")]) == 2
    L.save_miss_label(t, "w1", "noise", "")                                 # the owner labels one
    again = [window(1, "20261111T100000.000Z"), window(3, "20261111T120000.000Z")]
    assert Q.append_window_rows(t, again) == 1                              # only the new (device, utc)
    assert Q.append_window_rows(t, again) == 0
    rows = L.read_miss_table(t)
    assert [r["id"] for r in rows] == ["w1", "w2", "w3"] and rows[0]["label"] == "noise"


def test_group_times_and_replay_counts_per_device_and_evening(monkeypatch):
    assert Q.group_times([100.0, 103.0, 120.0, 90.0]) == [90.0, 100.0, 120.0]
    import kivi_replay_mine as K
    monkeypatch.setattr(K, "local_hour", lambda t: int(t))                   # the "epoch" is the hour
    info = [{"device": "c3-a", "hours": 10.0, "evening_h": 6.0, "live_dets": [10, 19, 23]},
            {"device": "c3-a", "hours": 2.0, "evening_h": 0.0, "live_dets": []},
            {"device": "c3-b", "hours": 5.0, "evening_h": 3.0, "live_dets": [20]}]
    c = Q.replay_counts(info)
    assert c["c3-a"] == {"hours": 12.0, "evening_h": 6.0, "false_wakes": 3, "false_wakes_evening": 2, "per_evening": 2.0}
    assert c["c3-b"]["per_evening"] == 2.0 and c["c3-b"]["false_wakes_evening"] == 1


def test_summary_has_one_row_per_day_replaced_on_rerun(tmp_path):
    p = tmp_path / "summary.md"
    js = {"role": "test", "disk_free_gb": 80.0, "live_wakes": {"by_label": {"c3-a true": 2, "c3-b open": 1}},
          "replay": {"per_device": {"c3-a": {"hours": 20.0, "false_wakes_evening": 1}}, "false_wake_windows_added": 1,
                     "miss_candidates_added": 3}}
    Q.update_summary(p, "2026-11-11", Q.summary_row("2026-11-11", js))
    Q.update_summary(p, "2026-11-10", Q.summary_row("2026-11-10", js))
    Q.update_summary(p, "2026-11-11", Q.summary_row("2026-11-11", js))
    rows = [ln for ln in p.read_text().splitlines() if ln.startswith("| 20")]
    assert [r.split(" | ")[0] for r in rows] == ["| 2026-10-10"[:0] + "| 2026-11-10", "| 2026-11-11"]
    assert "open 1, true 2" in rows[1] and "c3-a 1" in rows[1]


def test_lock_second_holder_exits_3(tmp_path):
    with Q.Lock(tmp_path / "l.lock"):
        with pytest.raises(SystemExit) as e:
            with Q.Lock(tmp_path / "l.lock"):
                pass
    assert e.value.code == 3
    with Q.Lock(tmp_path / "l.lock"):                                        # free again afterwards
        pass


# ---------------------------------------------------------------------------------------------
# whole run with a fake step runner
# ---------------------------------------------------------------------------------------------

LIVE = {"id": "oye_kivi_next", "version": "v5", "sha256": "ab" * 32, "raw": 0.9754, "file": "/x"}
NOW = 1794481200.0                                                           # 2026-11-12 12:00 Madrid


class FakeRunner:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def __call__(self, cmd, capture_output, text, env):
        i = [k for k, c in enumerate(cmd) if c.endswith(".py")][0]
        script, what = cmd[i].rsplit("/", 1)[1], cmd[i + 1]
        self.calls.append((script, what, cmd[i + 2:]))
        if self.fail == script:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        out = ""
        if script == "kivi_negatives.py":
            out = "  c3-a: 20 files, 5 closed, picked 3 (3 pieces, 1.50 h)\n  c3-b: 9 files, 2 closed, picked 1 (1 pieces, 0.50 h)\n"
        if script == "kivi_collect.py" and what == "_split":
            out = "RESULT " + json.dumps({"split_set": 0, "live": {"by_label": {"c3-a true": 1}}}) + "\n"
        if script == "kivi_collect.py" and what == "_replay":
            out = "RESULT " + json.dumps({"per_device": {"c3-a": {"hours": 1.5, "false_wakes_evening": 0}},
                                         "false_wake_windows_added": 0, "miss_candidates_added": 2}) + "\n"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(Q, "DAILY", tmp_path / "daily")
    monkeypatch.setattr(Q, "ROOT", tmp_path / "ds")
    (tmp_path / "ds" / "live").mkdir(parents=True)
    L.write_table(tmp_path / "ds" / "live" / "live_wakes.csv", [row(1, "20261111T100000.000Z", "true", source="auto-v1")])
    monkeypatch.setattr(Q, "free_gb", lambda path=None: 80.0)
    monkeypatch.setattr(Q, "vram_free_gb", lambda: 14.0)
    monkeypatch.setattr(Q, "heavy_jobs", lambda *a, **k: [])
    monkeypatch.setattr(Q, "read_live_id", lambda ctx="grigri": "oye_kivi_next")
    monkeypatch.setattr(Q, "live_model", lambda wid, models=None: dict(LIVE))
    return tmp_path


def args(**kw):
    base = dict(day=None, from_day=None, redo=False, dry_run=False, max_heavy=2, wait_min=0.0, jobs=6, miss_limit=6,
                context="grigri")
    return Namespace(**(base | kw))


def test_run_processes_yesterday_only_once_and_records_counts(env):
    r = FakeRunner()
    assert Q.cmd_run(args(), runner=r, out=lambda *_: None, now=NOW) == 0
    steps = [(c[0], c[1]) for c in r.calls]
    assert steps == [("kivi_live_wakes.py", "extract"), ("kivi_collect.py", "_split"), ("kivi_negatives.py", "--local-date"),
                     ("kivi_collect.py", "_replay")]                         # the only row is labelled: autolabel not run
    neg = [c for c in r.calls if c[0] == "kivi_negatives.py"][0][2]
    assert neg[:5] == ["2026-11-11", "--part", "d-20261111", "--out-name", "negatives-daily"] and "Kiwi Test" in neg
    js = json.loads((env / "daily" / "2026-11-11.json").read_text())
    assert js["role"] == "test" and js["room_copy"]["c3-a"] == {"pieces": 3, "hours": 1.5}
    assert js["live_model"]["id"] == "oye_kivi_next" and js["steps"]["autolabel"] == "nothing to label"
    st = json.loads((env / "daily" / "state.json").read_text())
    assert st["start"] == "2026-11-11" and st["days"]["2026-11-11"]["done"] is True
    assert "| 2026-11-11 | test |" in (env / "daily" / "summary.md").read_text()
    # the day is now done (processed 12 h after its midnight, not early): a second run does nothing
    r2 = FakeRunner()
    assert Q.cmd_run(args(), runner=r2, out=lambda *_: None, now=NOW + 3600) == 0 and r2.calls == []
    assert Q.cmd_run(args(day="2026-11-11"), runner=r2, out=lambda *_: None, now=NOW) == 0 and r2.calls == []
    assert Q.cmd_run(args(day="2026-11-11", redo=True), runner=r2, out=lambda *_: None, now=NOW) == 0 and len(r2.calls) == 4
    assert len((env / "daily" / "summary.md").read_text().count("| 2026-11-11 |") * [1]) == 1


def test_run_catches_up_all_missed_days_up_to_yesterday(env):
    st = {"start": "2026-11-08", "days": {"2026-11-08": {"done": True, "early": False}}}
    Q.save_state(st)
    r = FakeRunner()
    assert Q.cmd_run(args(), runner=r, out=lambda *_: None, now=NOW) == 0
    done = json.loads((env / "daily" / "state.json").read_text())["days"]
    assert sorted(d for d, s in done.items() if s["done"]) == ["2026-11-08", "2026-11-09", "2026-11-10", "2026-11-11"]
    roles = [json.loads((env / "daily" / f"{d}.json").read_text())["role"] for d in ("2026-11-09", "2026-11-10", "2026-11-11")]
    assert roles == ["test", "mine", "test"]


def test_run_marks_a_day_collected_too_early_and_repeats_it(env):
    midnight_after = Q.day_end_epoch("2026-11-11")
    r = FakeRunner()
    assert Q.cmd_run(args(day="2026-11-11"), runner=r, out=lambda *_: None, now=midnight_after + 2 * 3600) == 0
    st = json.loads((env / "daily" / "state.json").read_text())
    assert st["days"]["2026-11-11"]["done"] is True and st["days"]["2026-11-11"]["early"] is True
    assert Q.pending_days(st, date(2026, 11, 12)) == ["2026-11-11"]                     # comes back
    assert Q.cmd_run(args(), runner=r, out=lambda *_: None, now=midnight_after + 7 * 3600) == 0
    st = json.loads((env / "daily" / "state.json").read_text())
    assert st["days"]["2026-11-11"]["early"] is False and Q.pending_days(st, date(2026, 11, 12)) == []


def test_run_refuses_today_and_failed_step_keeps_the_day_pending(env):
    with pytest.raises(SystemExit):
        Q.cmd_run(args(day="2026-11-12"), runner=FakeRunner(), out=lambda *_: None, now=NOW)
    r = FakeRunner(fail="kivi_negatives.py")
    assert Q.cmd_run(args(day="2026-11-11"), runner=r, out=lambda *_: None, now=NOW) == 2
    st = json.loads((env / "daily" / "state.json").read_text())
    assert st["days"]["2026-11-11"]["done"] is False and st["days"]["2026-11-11"]["failed"] == "room copy"
    assert not any(c[1] == "_replay" for c in r.calls) and not (env / "daily" / "summary.md").exists()
    assert Q.pending_days(st, date(2026, 11, 12)) == ["2026-11-11"]


def test_run_disk_guard_and_heavy_guard_do_nothing(env, monkeypatch):
    r = FakeRunner()
    monkeypatch.setattr(Q, "free_gb", lambda path=None: 49.0)
    assert Q.cmd_run(args(day="2026-11-11"), runner=r, out=lambda *_: None, now=NOW) == 5 and r.calls == []
    monkeypatch.setattr(Q, "free_gb", lambda path=None: 80.0)
    monkeypatch.setattr(Q, "heavy_jobs", lambda *a, **k: [1, 2])
    assert Q.cmd_run(args(day="2026-11-11"), runner=r, out=lambda *_: None, now=NOW) == 4 and r.calls == []
    monkeypatch.setattr(Q, "heavy_jobs", lambda *a, **k: [1])                # one other job + this = 2: allowed
    assert Q.cmd_run(args(day="2026-11-11", max_heavy=1), runner=r, out=lambda *_: None, now=NOW) == 4
    assert Q.cmd_run(args(day="2026-11-11"), runner=r, out=lambda *_: None, now=NOW) == 0 and r.calls


def test_dry_run_only_plans_the_room_copy(env):
    r = FakeRunner()
    assert Q.cmd_run(args(day="2026-11-11", dry_run=True), runner=r, out=lambda *_: None, now=NOW) == 0
    assert [c[0] for c in r.calls] == ["kivi_negatives.py"] and "--dry-run" in r.calls[0][2]
    assert not (env / "daily" / "summary.md").exists()


def test_scope_wrapper_caps_resources():
    assert Q.SCOPE[:4] == ["systemd-run", "--user", "--scope", "-q"] and "MemoryMax=12G" in Q.SCOPE
    assert "CPUQuota=800%" in Q.SCOPE and Q.SCOPE[-3:] == ["nice", "-n", "10"]
