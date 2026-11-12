#!/usr/bin/env python3
"""Owner-triggered data loop of the "oye Kivi" wake word: collect every Europe/Madrid day not yet processed.

    kivi_collect.py status                       # live model, days done / pending, queue sizes, disk, heavy jobs (read only)
    kivi_collect.py run                          # every pending day up to yesterday
    kivi_collect.py run --day 2026-11-11 [--redo]  # one day (idempotent; --redo runs a processed day again)
    kivi_collect.py run --dry-run                # plan only: pending days, what the room copy would take, disk
    kivi_collect.py run --from 2026-11-12        # first run only: the first day of the loop (default: yesterday)
    kivi_collect.py prune [--older-than-days 45] # LIST the raw room audio that is old enough to delete (nothing is deleted)
    kivi_collect.py prune --yes                  # delete exactly that list (owner decision, never automatic)

There is no timer: nothing runs unless the owner (or an agent the owner asked, see the kivi-wakeword-loop skill) starts it.

Per day D (Madrid calendar day; odd day of the month = TEST, even = train / selection, never mixed):
  1. live wakes of D: `kivi_live_wakes.py extract --day D`, then `autolabel --day D` (rows without a label only; owner
     labels are never touched; trigger classification is part of extract), then the split column by date parity;
  2. room audio of D: `kivi_negatives.py --local-date D --out-name negatives-daily --part d-YYYYMMDD`;
  3. replay of that audio with the LIVE model (the wake word id of the pipeline "Kiwi", read from
     `.storage/assist_pipeline.pipelines`, read only; its file and raw threshold from kivi-models) next to v1 and v4:
     false-wake events of the live model -> `live-misses/daily_false_wakes.csv`, near-misses (v4 or live peak >= 0.5,
     live below its threshold, best rank first, at most --miss-limit per day) -> `live-misses/daily_miss_candidates.csv`,
     both in the hidden-score table format of `kivi_live_wakes.py queue`; rows already there are never changed;
  4. counts only: `daily/YYYY-MM-DD.json` and one row in `daily/summary.md`.
A day is marked done in `daily/state.json` only when every step succeeded. A day collected less than 6 h after its midnight
is marked `early` and runs again later (a recording file that spans midnight is copied once it is closed).

Where the room audio lives: the raw room-audio sets (`negatives*`) are symlinks in `datasets/real/` to `/datasets/kivi-room-audio/`
(the second ZFS pool, owner decision 2026-11-12); new daily copies land there through the `negatives-daily` symlink (or
`$KIVI_ROOM_DIR`, which `run` links to when `negatives-daily` does not exist). The 50 GB guard checks the pool that receives
the copy. `prune` lists room-audio pieces older than 45 days (Madrid date of the piece) and deletes only with `--yes`;
it never touches labels, scores, window clips, or pieces of a protected day or part (`daily/protect.json`, seeded with
2026-11-13, the first fresh odd test day after 2026-11-12 was released for training (owner decision 2026-11-12), and the
part `test2-v5era`) or of a day with an open review item.

Safety: one lock (`daily/collect.lock`); at most --max-heavy (default 2) heavy jobs in total, this one included (v6_replay,
kivi_replay_mine, training, autolabel, another collect): it waits up to --wait-min minutes, else exits 4; refuses to copy
when less than 50 GB are free (exit 5; nothing is ever deleted); every python step runs in
`systemd-run --user --scope -p MemoryMax=12G -p CPUQuota=800%` under nice 10 with at most 8 threads; autolabel needs
6 GB of free VRAM (its models use about 3 GB; it is skipped otherwise and the day stays pending).
Privacy: counts, times and levels only; no audio is played, no transcript or speaker embedding leaves the autolabel process.
Exit codes: 0 done (or nothing to do), 2 a step failed, 3 locked, 4 heavy jobs running, 5 disk.
"""
import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kivi_real_common as _C  # noqa: E402

WORK = _C.kivi_home()                      # $KIVI_HOME, default ~/kivi-models/data
ROOT = WORK / "datasets/real"
DAILY = WORK / "daily"
PY = os.environ.get("WAKE_PY", str(WORK / ".venv/bin/python"))
KIVI_MODELS = _C.kivi_models_repo()
HERE = Path(__file__).resolve().parent
TZ = "Europe/Madrid"
SET = "negatives-daily"
SCORES = "scores/daily-replay"
MIN_FREE_GB = 50.0
EARLY_H = 6.0
MISS_LIMIT = 6
MISS_FLOOR = 0.5
MERGE_FW_S = 6.0
PIPELINE = "Kiwi"                          # the household pipeline; "Kiwi Test" is never read
FALSE_WAKES = "daily_false_wakes.csv"
MISSES = "daily_miss_candidates.csv"
SCOPE = ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=12G", "-p", "CPUQuota=800%", "--", "nice", "-n", "10"]
HEAVY_RE = re.compile(r"(v6_replay|kivi_replay_mine|v6_room|v6p?[0-9]*_(train|compare|stage|eval)|train_driver|train_simple|"
                      r"/train\.sh|kivi_live_wakes\.py (autolabel|extract)|kivi_negatives|kivi_collect\.py (run|_))")
MODELS_ENV = "KIVI_COLLECT_MODELS"


# ---------------------------------------------------------------------------------------------
# days, state
# ---------------------------------------------------------------------------------------------


def madrid_today(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now, ZoneInfo(TZ)).date()


def role_of(day):
    import kivi_real_common as C
    return C.day_role(date.fromisoformat(day) if isinstance(day, str) else day)


def day_end_epoch(day):
    d = date.fromisoformat(day) + timedelta(days=1)
    return datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(TZ)).timestamp()


def load_state(daily=None):
    p = Path(daily or DAILY) / "state.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"start": None, "days": {}}


def save_state(state, daily=None):
    p = Path(daily or DAILY) / "state.json"
    atomic_write(p, json.dumps(state, indent=1, sort_keys=True) + "\n")


def atomic_write(path, text):
    p = Path(path)
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def pending_days(state, today):
    """Madrid days from state["start"] up to yesterday that are not done, plus days done too early. [] without a start."""
    if not state.get("start"):
        return []
    out, d, last = [], date.fromisoformat(state["start"]), today - timedelta(days=1)
    while d <= last:
        s = state["days"].get(d.isoformat())
        if not s or not s.get("done") or s.get("early"):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------------------------
# safety: heavy jobs, disk, lock
# ---------------------------------------------------------------------------------------------


WATCHER_CMDS = {"pgrep", "pkill", "ps", "grep", "egrep", "tail", "cat", "sleep", "timeout", "tmux", "screen", "watch", "less", "vim",
                "nvim", "nano", "git", "rg", "ls", "head", "wc", "tee"}
SHELLS = {"sh", "bash", "zsh", "dash", "fish"}


def _watcher(args):
    """True for a process that only mentions a heavy job's name: a `bash -c` / `zsh -c` line, a waiter loop, grep, tail, an
    editor. A launcher script (`bash /path/job.sh`) and the python of a job are not watchers."""
    toks = args.split()
    first = os.path.basename(toks[0]) if toks else ""
    if first in WATCHER_CMDS:
        return True
    return first in SHELLS and any(t == "-c" or (t.startswith("-") and not t.startswith("--") and "c" in t) for t in toks[1:3])


def heavy_jobs(ps_lines=None, own_pid=None):
    """Number of heavy jobs running, as process trees: a launcher script and the python it started count once. Lines are
    `pid ppid args`. This process and its ancestors are not counted."""
    if ps_lines is None:
        ps_lines = subprocess.run(["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True).stdout.splitlines()
    procs = {}
    for ln in ps_lines:
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", ln)
        if m:
            procs[int(m.group(1))] = (int(m.group(2)), m.group(3))
    me, skip = own_pid or os.getpid(), set()
    while me in procs:                       # ourselves and our parents (the agent's shell mentions the script name)
        skip.add(me)
        me = procs[me][0]
    heavy = {pid for pid, (_pp, args) in procs.items() if pid not in skip and HEAVY_RE.search(args) and not _watcher(args)}
    return sorted(pid for pid in heavy if procs[pid][0] not in heavy)


def free_gb(path=None):
    return shutil.disk_usage(str(path or WORK)).free / 1e9


def room_dir():
    """Where the daily room audio is written: `datasets/real/negatives-daily` (a symlink to /datasets/kivi-room-audio).
    With $KIVI_ROOM_DIR and no such path yet, link it to `$KIVI_ROOM_DIR/negatives-daily`."""
    p = ROOT / SET
    env = os.environ.get("KIVI_ROOM_DIR")
    if env and not os.path.lexists(p):
        target = Path(env).expanduser() / SET
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.symlink(target, p)
    return p


def room_free_gb():
    """Free GB of the pool that receives the room copy (the target of the `negatives-daily` link, else of its parent)."""
    p = room_dir()
    return free_gb(p if p.exists() else p.parent)


def vram_free_gb():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"], capture_output=True,
                           text=True, timeout=20)
        return min(float(x) for x in r.stdout.split()) / 1024
    except Exception:       # noqa: BLE001
        return 0.0


class Lock:
    def __init__(self, path):
        self.path = Path(path)
        self.f = None

    def __enter__(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.f = open(self.path, "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close()
            raise SystemExit(3)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


# ---------------------------------------------------------------------------------------------
# live model (read only)
# ---------------------------------------------------------------------------------------------


def live_wake_id(pipelines_json):
    """Wake word id of the pipeline named PIPELINE, from the text of `assist_pipeline.pipelines`; nothing else is kept."""
    items = json.loads(pipelines_json)["data"]["items"]
    ids = [i.get("wake_word_id") for i in items if i.get("name") == PIPELINE]
    if len(ids) != 1 or not ids[0]:
        raise SystemExit(f"cannot tell the live wake word: {len(ids)} pipeline(s) named {PIPELINE!r}")
    return ids[0]


def read_live_id(context="grigri"):
    r = subprocess.run(["kubectl", "--context", context, "-n", "home-assistant", "exec", "home-assistant-hass-0", "--", "cat",
                        "/config/.storage/assist_pipeline.pipelines"], capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise SystemExit("cannot read the pipelines (read only kubectl exec failed)")
    return live_wake_id(r.stdout)


def live_model(wake_id, models=KIVI_MODELS):
    """{id, file, version, sha256, raw}: the uncalibrated file of the version in models.lock and its server raw point."""
    for ln in (Path(models) / "models.lock").read_text().splitlines():
        parts = ln.split()
        if len(parts) == 3 and not ln.startswith("#") and parts[0] == wake_id:
            d = Path(models) / "wakeword" / wake_id / parts[1]
            f = d / f"{wake_id}_uncalibrated.tflite"
            raw = json.loads((d / f"{wake_id}_uncalibrated.calibration.json").read_text())["server_0_5_raw"]
            import hashlib
            return {"id": wake_id, "file": str(f), "version": parts[1], "raw": float(raw),
                    "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
    raise SystemExit(f"wake word {wake_id!r} is not in models.lock")


def replay_models(live):
    """{name: file} scored each night: v1, v4, v5 as in the v5-era mining, plus the live model if it is none of them."""
    import kivi_replay_mine as K
    models = dict(K.MODEL_FILES)
    if live["id"] not in models:
        models[live["id"]] = Path(live["file"])
    return {k: str(v) for k, v in models.items()}


# ---------------------------------------------------------------------------------------------
# pure per-day aggregation
# ---------------------------------------------------------------------------------------------


def group_times(times, gap_s=MERGE_FW_S):
    """Detections closer than gap_s to the previous one are one event; returns the first time of each."""
    out, last = [], None
    for t in sorted(times):
        if last is None or t - last > gap_s:
            out.append(t)
        last = t
    return out


def replay_counts(pieces_info):
    """pieces_info: [{device, hours, evening_h, live_dets: [epoch]}] -> per device counts (false wakes of the live model)."""
    import kivi_replay_mine as K
    dev = defaultdict(lambda: {"hours": 0.0, "evening_h": 0.0, "false_wakes": 0, "false_wakes_evening": 0})
    for p in pieces_info:
        c = dev[p["device"]]
        c["hours"] += p["hours"]
        c["evening_h"] += p["evening_h"]
        c["false_wakes"] += len(p["live_dets"])
        c["false_wakes_evening"] += sum(1 for t in p["live_dets"] if 18 <= K.local_hour(t) < 24)
    out = {}
    for d, c in sorted(dev.items()):
        ev = c["evening_h"] / 6.0
        out[d] = {"hours": round(c["hours"], 3), "evening_h": round(c["evening_h"], 3), "false_wakes": c["false_wakes"],
                  "false_wakes_evening": c["false_wakes_evening"],
                  "per_evening": round(c["false_wakes_evening"] / ev, 3) if ev else None}
    return out


def live_label_counts(rows, day):
    """Live wakes of a Madrid day by device and label (`open` = no label yet), plus trigger and source counts."""
    import kivi_live_wakes as L
    mine = [r for r in rows if L.local_day(r["utc"]) == day]
    by_label = Counter((r["device"], r["label"] or "open") for r in mine)
    return {"rows": len(mine),
            "by_label": {f"{d} {lab}": n for (d, lab), n in sorted(by_label.items())},
            "by_trigger": dict(Counter(r["trigger"] or "empty" for r in mine)),
            "owner_labelled": sum(1 for r in mine if r["label"] and not r["label_source"].startswith("auto")),
            "split": dict(Counter(r["split"] or "none" for r in mine))}


def assign_split(rows, day):
    """Set `split` by date parity on the day's rows labelled true or false (odd = test, even = train). Returns the count."""
    import kivi_live_wakes as L
    role = "test" if role_of(day) == "test" else "train"
    n = 0
    for r in rows:
        if L.local_day(r["utc"]) == day and r["label"] in ("true", "false"):
            r["split"] = role
            n += 1
    return n


def append_window_rows(table, rows):
    """Append rows (hidden-score format) whose (device, utc) is not in the table yet, from a fresh read; atomic. Existing
    rows, labelled or not, are never changed. Returns the number added."""
    import kivi_live_wakes as L
    table = Path(table)
    cur = L.read_miss_table(table)
    have = {(r["device"], r["utc"]) for r in cur}
    new = [r for r in rows if (r["device"], r["utc"]) not in have]
    if new:
        L.write_miss_table(table, cur + new)
    return len(new)


def summary_row(day, js):
    lab = js["live_wakes"]["by_label"]
    tot = Counter()
    for k, n in lab.items():
        tot[k.split(" ", 1)[1]] += n
    rp = js["replay"]
    fw = " / ".join(f"{d} {v['false_wakes_evening']}" for d, v in sorted(rp["per_device"].items())) or "-"
    return (f"| {day} | {js['role']} | " + ", ".join(f"{k} {v}" for k, v in sorted(tot.items())) + " | "
            f"{sum(v['hours'] for v in rp['per_device'].values()):.1f} | {fw} | {rp['false_wake_windows_added']} | "
            f"{rp['miss_candidates_added']} | {js['disk_free_gb']:.0f} |")


SUMMARY_HEAD = ("| day | role | live wakes by label | room h | live-model false wakes in the evening per device | "
                "false-wake windows added | miss candidates added | free GB |\n| --- | --- | --- | --- | --- | --- | --- | --- |\n")


def update_summary(path, day, row):
    """One line per day, replaced when the day is run again; sorted by day."""
    p = Path(path)
    lines = [ln for ln in (p.read_text().splitlines() if p.exists() else []) if ln.startswith("| 20")]
    lines = [ln for ln in lines if not ln.startswith(f"| {day} |")] + [row]
    atomic_write(p, "# Daily collection summary (counts only)\n\n" + SUMMARY_HEAD + "\n".join(sorted(lines)) + "\n")


# ---------------------------------------------------------------------------------------------
# steps that run as python under the scope (subcommands `_split`, `_replay`)
# ---------------------------------------------------------------------------------------------


def _models_from_env():
    return json.loads(os.environ[MODELS_ENV]) if os.environ.get(MODELS_ENV) else None


def _install_models(models):
    import kivi_replay_mine as K
    K.MODEL_FILES = {k: Path(v) for k, v in models.items()}
    K.NAMES = tuple(models)


def _worker_init(models):
    import kivi_replay_mine as K
    _install_models(models)
    K._W["rt"] = K.load_runtime()


def chunk_dir(models):
    """Range files of one model set: `chunks` for the v1 / v4 / v5 set (the files before 2026-11-12), `chunks-<extra>` when
    the live model adds a column (a range file holds one column per model, so two sets never share a directory)."""
    import kivi_replay_mine as K
    names = list(models)
    base = list(K.DEFAULT_MODEL_FILES)
    extra = [n for n in names if n not in base]
    return ROOT / SCORES / ("chunks" if names == base else "chunks-" + "+".join(extra or names))


def _score_day(day, part, models, jobs, out=print):
    """Score the day's pieces (resumable range files under scores/daily-replay/chunks); returns (pieces, ranges done now)."""
    import multiprocessing as mp
    import kivi_real_common as C
    import kivi_replay_mine as K
    pieces = [p for p in K.read_pieces(ROOT, SET) if p["part"] == part]
    cdir = chunk_dir(models)
    if not cdir.exists():
        C.private_mkdir(cdir, ROOT)
    tasks = K.plan_tasks(pieces, 1200.0)
    src0 = {p["clip"]: p["src0"] for p in pieces}
    todo = [(c, p, s0, s1, 60.0, str(cdir / K.chunk_name(c, s0, src0[c])), src0[c]) for c, p, s0, s1 in tasks
            if not (cdir / K.chunk_name(c, s0, src0[c])).exists()]
    off = sum(1 for p in pieces if p["grid"] != "exact")
    out(f"replay {day}: {len(pieces)} piece(s), {sum(p['n'] for p in pieces) / 16000 / 3600:.2f} h, {len(todo)} range(s) to score"
        + (f"; {off} piece(s) without an exact server grid (replayed on their own grid)" if off else ""))
    if todo:
        with mp.get_context("spawn").Pool(jobs, initializer=_worker_init, initargs=(models,)) as pool:
            for _ in pool.imap_unordered(K._task, todo):
                pass
    return pieces, len(todo)


def cmd_replay(a, out=print):
    """Score, then false wakes / near-misses of the live model into the two tables; prints one JSON line with the counts."""
    import kivi_live_wakes as L
    import kivi_autolabel as A
    import kivi_real_common as C
    import numpy as np
    import kivi_replay_mine as K
    live = json.loads(a.live)
    models = replay_models(live)
    _install_models(models)
    names = list(models)
    k_live, k_v1, k_v4 = names.index(live["id"]), names.index("kivi"), names.index("oye_kivi")
    part = f"d-{a.day.replace('-', '')}"
    pieces, scored = _score_day(a.day, part, models, a.jobs, out)
    chunks = chunk_dir(models)
    by_clip = defaultdict(list)
    for t in K.plan_tasks(pieces, 1200.0):
        by_clip[t[0]].append(t)
    info, fw_windows, pool = [], [], []
    for pc in pieces:
        t, p, _steps = K.load_piece_steps(chunks, pc["clip"], by_clip[pc["clip"]], pc["src0"])
        dets = [pc["start"] + x for x in K.detections_of(t, p[:, k_live], live["raw"])]
        info.append({"device": pc["device"], "hours": pc["n"] / 16000 / 3600,
                     "evening_h": K.local_overlap_hours(pc["start"], pc["start"] + pc["n"] / 16000), "live_dets": dets})
        fw_windows += [(pc, x) for x in group_times(dets)]
        for e in K.cluster_events(t, p):
            if e["peak"][k_live] > live["raw"] or max(e["peak"][k_v4], e["peak"][k_live]) < MISS_FLOOR:
                continue
            tp = e["tpeak"][k_live] if e["peak"][k_live] >= e["peak"][k_v4] else e["tpeak"][k_v4]
            pool.append({"device": pc["device"], "part": part, "piece": pc, "t_abs": pc["start"] + tp,
                         "peak": [e["peak"][k_v1], e["peak"][k_v4], e["peak"][k_live]]})
    misses = ROOT / "live-misses"
    if not misses.exists():
        C.private_mkdir(misses, ROOT)

    def cut(pc, t_abs, sub):
        utc = L.dir_stamp(t_abs)
        rel = f"{part}/{sub}/{pc['device']}/{utc}/window.wav"
        C.private_mkdir(misses / part / sub / pc["device"] / utc, ROOT)
        if (misses / rel).exists():
            rms, pk = K.wav_levels(np.fromfile(misses / rel, dtype="<i2", offset=44))
        else:
            first, last = K.window_bounds(t_abs, pc["start"], pc["n"])
            rms, pk = K.cut_window(pc["path"], first, last, misses / rel)
        return utc, rel, rms, pk

    fw_rows = []
    for i, (pc, x) in enumerate(sorted(fw_windows, key=lambda w: (w[1], w[0]["device"])), 1):
        utc, rel, rms, pk = cut(pc, x, "fw")
        fw_rows.append({k: "" for k in L.MISS_FIELDS} | {"id": f"dfw-{part[2:]}-{i:03d}", "device": pc["device"], "part": part,
                        "utc": utc, "rank": "0", "level_dbfs": rms, "peak_dbfs": pk, "clip": rel})
    live_t = K.live_detection_times(ROOT)
    stt_t = K.stt_only_times(L.get_fetcher(a), L.devices_prefix(a))
    K.rank_pool(pool, live_t, stt_t)
    chosen = sorted(K.select_candidates(pool, a.miss_limit), key=lambda c: -c["rank"])
    mc_rows = []
    for i, c in enumerate(chosen, 1):
        utc, rel, rms, pk = cut(c["piece"], c["t_abs"], "mc")
        mc_rows.append({k: "" for k in L.MISS_FIELDS} | {
            "id": f"dmc-{part[2:]}-{i:03d}", "device": c["device"], "part": part, "utc": utc, "peak_v1": round(c["peak"][0], 4),
            "peak_v4": round(c["peak"][1], 4), "peak_v5": round(c["peak"][2], 4), "retry": c["retry"]["kind"],
            "retry_detail": c["retry"]["detail"], "rank": c["rank"], "level_dbfs": rms, "peak_dbfs": pk, "clip": rel})
    if not A.wait_for_review(None, out=out):
        raise SystemExit("a labelling session is running: the tables were not written")
    added_fw = append_window_rows(misses / FALSE_WAKES, fw_rows)
    added_mc = append_window_rows(misses / MISSES, mc_rows)
    res = {"pieces": len(pieces), "ranges_scored_now": scored, "per_device": replay_counts(info),
           "false_wake_events": len(fw_rows), "false_wake_windows_added": added_fw, "miss_pool": len(pool),
           "miss_candidates_selected": len(mc_rows), "miss_candidates_added": added_mc}
    print("RESULT " + json.dumps(res))
    return res


def cmd_split(a, out=print):
    import kivi_live_wakes as L
    import kivi_autolabel as A
    table = ROOT / "live" / "live_wakes.csv"
    if not table.exists():
        print("RESULT " + json.dumps({"split_set": 0}))
        return 0
    if not A.wait_for_review(None, out=out):
        raise SystemExit("a labelling session is running: the split was not written")
    rows = L.read_table(table)                       # fresh read right before the write
    n = assign_split(rows, a.day)
    L.write_table(table, rows)
    print("RESULT " + json.dumps({"split_set": n, "live": live_label_counts(rows, a.day)}))
    return n


# ---------------------------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------------------------


def scoped(cmd, env=None, runner=None):
    """Run a python step in the resource scope; returns (rc, stdout). Output lines are echoed (counts only by design)."""
    full = SCOPE + cmd
    e = dict(os.environ, OMP_NUM_THREADS="8", OPENBLAS_NUM_THREADS="8", MKL_NUM_THREADS="8", **(env or {}))
    r = (runner or subprocess.run)(full, capture_output=True, text=True, env=e)
    for ln in r.stdout.splitlines():
        print("    " + ln)
    if r.returncode:
        for ln in r.stderr.splitlines()[-6:]:
            print("    ! " + ln[:200])
    return r.returncode, r.stdout


def result_of(stdout):
    for ln in reversed(stdout.splitlines()):
        if ln.startswith("RESULT "):
            return json.loads(ln[7:])
    return {}


def py(script, *args):
    return [PY, "-I", str(HERE / script), *map(str, args)]


def collect_day(day, a, live, runner=None, out=print):
    """All steps of one day. Returns (ok, the JSON record, failed step)."""
    part = f"d-{day.replace('-', '')}"
    rec = {"day": day, "role": role_of(day), "live_model": {k: live[k] for k in ("id", "version", "sha256", "raw")},
           "steps": {}, "collected_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    common = ["--context", a.context]

    def step(name, cmd, env=None):
        rc, so = scoped(cmd, env, runner)
        rec["steps"][name] = "ok" if rc == 0 else f"failed({rc})"
        return rc == 0, so

    ok, _ = step("extract", py("kivi_live_wakes.py", "extract", "--day", day, *common))
    if not ok:
        return False, rec, "extract"
    import kivi_live_wakes as L
    rows = L.read_table(ROOT / "live" / "live_wakes.csv")
    open_rows = [r for r in rows if L.local_day(r["utc"]) == day and not r["label"]]
    if open_rows:
        if vram_free_gb() < 6.0:
            rec["steps"]["autolabel"] = "skipped(vram)"
            return False, rec, "autolabel (less than 6 GB of VRAM free)"
        ok, _ = step("autolabel", py("kivi_live_wakes.py", "autolabel", "--day", day, *common))
        if not ok:
            return False, rec, "autolabel"
    else:
        rec["steps"]["autolabel"] = "nothing to label"
    ok, so = step("split", py("kivi_collect.py", "_split", "--day", day))
    if not ok:
        return False, rec, "split"
    rec["live_wakes"] = result_of(so).get("live", {})
    if room_free_gb() < MIN_FREE_GB:
        rec["steps"]["room copy"] = "refused(disk)"
        return False, rec, f"room copy (less than {MIN_FREE_GB:.0f} GB free)"
    ok, so = step("room copy", py("kivi_negatives.py", "--local-date", day, "--part", part, "--out-name", SET, "--hours", "1000",
                                  "--exclude-pipeline", PIPELINE + " Test", "--context", a.context))
    if not ok:
        return False, rec, "room copy"
    m = re.findall(r"(c3-\w): \d+ files, \d+ closed, picked \d+ \((\d+) pieces, ([\d.]+) h\)", so)
    rec["room_copy"] = {d: {"pieces": int(n), "hours": float(h)} for d, n, h in m}
    ok, so = step("replay", py("kivi_collect.py", "_replay", "--day", day, "--live", json.dumps(live), "--jobs", a.jobs,
                               "--miss-limit", a.miss_limit, *common))
    if not ok:
        return False, rec, "replay"
    rec["replay"] = result_of(so)
    rec["queue_pending_for_day"] = {f"{L.group_number(g)}. {n}": c for (g, n), c in L.queue_counts(L.queue_items(ROOT, day=day)).items()}
    rec["disk_free_gb"] = round(room_free_gb(), 1)
    return True, rec, ""


def cmd_status(a, out=print):
    import kivi_live_wakes as L
    state = load_state()
    today = madrid_today()
    out(f"today (Madrid) {today}; loop start {state.get('start') or 'not set (first run sets it: yesterday, or --from)'}")
    try:
        live = live_model(read_live_id(a.context))
        out(f"live model: {live['id']} {live['version']} sha256 {live['sha256'][:12]} raw threshold {live['raw']}")
    except SystemExit as e:
        out(f"live model: unknown ({e})")
    done = sorted(d for d, s in state["days"].items() if s.get("done"))
    out(f"days done: {len(done)}" + (f" ({done[0]} .. {done[-1]})" if done else ""))
    pend = pending_days(state, today)
    out(f"days pending (up to yesterday): {len(pend)} " + ", ".join(f"{d} ({role_of(d)})" for d in pend))
    items = L.queue_items(ROOT)
    out(f"labelling queue: {len(items)} item(s)")
    for (g, name), n in L.queue_counts(items).items():
        out(f"  {L.group_number(g)}. {L.QUEUE_GROUPS[g]}: {name}: {n}")
    out(f"free disk {free_gb():.0f} GB on the work pool, {room_free_gb():.0f} GB where the room audio goes "
        f"(refuse to copy below {MIN_FREE_GB:.0f}); heavy jobs running: {len(heavy_jobs())}; "
        f"free VRAM {vram_free_gb():.1f} GB")
    return {"pending": pend, "queue": len(items)}


def cmd_run(a, runner=None, out=print, now=None):
    today = madrid_today(now)
    yesterday = (today - timedelta(days=1)).isoformat()
    with Lock(DAILY / "collect.lock"):
        state = load_state()
        if a.day:
            days = [a.day]
            if a.day > yesterday:
                raise SystemExit(f"{a.day} is not over yet (yesterday is {yesterday})")
            if not state.get("start"):
                state["start"] = a.day
        else:
            if not state.get("start"):
                state["start"] = getattr(a, "from_day", None) or yesterday
            days = pending_days(state, today)
        if a.day and not a.redo and state["days"].get(a.day, {}).get("done") and not state["days"][a.day].get("early"):
            out(f"{a.day} is already done (use --redo)")
            return 0
        if not days:
            out("nothing to do: no pending day")
            return 0
        out(f"days to collect: {', '.join(f'{d} ({role_of(d)})' for d in days)}")
        deadline = time.time() + a.wait_min * 60
        while True:
            n = len(heavy_jobs())
            if n + 1 <= a.max_heavy:
                break
            out(f"{n} heavy job(s) running, at most {a.max_heavy} in total with this one" + (": waiting" if time.time() < deadline else ""))
            if time.time() >= deadline:
                return 4
            time.sleep(min(300, max(1, deadline - time.time())))
        if room_free_gb() < MIN_FREE_GB:
            out(f"only {room_free_gb():.0f} GB free where the room audio goes (less than {MIN_FREE_GB:.0f}): refusing to copy")
            return 5
        live = live_model(read_live_id(a.context))
        out(f"live model {live['id']} {live['version']} raw {live['raw']}")
        if a.dry_run:
            for d in days:
                out(f"-- plan {d} ({role_of(d)}): extract, autolabel, split, room copy, replay")
                rc, _ = scoped(py("kivi_negatives.py", "--local-date", d, "--part", f"d-{d.replace('-', '')}", "--out-name", SET,
                                  "--hours", "1000", "--exclude-pipeline", PIPELINE + " Test", "--context", a.context, "--dry-run"),
                               runner=runner)
            return 0
        for d in days:
            out(f"== {d} ({role_of(d)})")
            ok, rec, failed = collect_day(d, a, live, runner, out)
            DAILY.mkdir(mode=0o700, parents=True, exist_ok=True)
            atomic_write(DAILY / f"{d}.json", json.dumps(rec, indent=1, sort_keys=True) + "\n")
            early = (time.time() if now is None else now) - day_end_epoch(d) < EARLY_H * 3600
            state["days"][d] = {"done": ok, "early": bool(ok and early), "at": rec["collected_utc"], "failed": failed}
            if ok:
                update_summary(DAILY / "summary.md", d, summary_row(d, rec))
                out(f"   done: live wakes {rec['live_wakes'].get('by_label')}; replay {rec['replay']['per_device']}; "
                    f"false-wake windows added {rec['replay']['false_wake_windows_added']}, "
                    f"miss candidates added {rec['replay']['miss_candidates_added']}" + (" (early: runs again later)" if early else ""))
            save_state(state)
            if not ok:
                out(f"   FAILED at {failed}; later days not started")
                return 2
        return 0


# ---------------------------------------------------------------------------------------------
# prune (owner decision: raw room audio older than 45 days; lists first, deletes only with --yes)
# ---------------------------------------------------------------------------------------------

PRUNE_DAYS = 45
PROTECT_SEED = {"days": ["2026-11-13"], "parts": ["test2-v5era"],
                "note": "2026-11-12 released for training and selection (owner decision 2026-11-12; the v6.1 reservation "
                        "is void); 2026-11-13 stays a fresh odd TEST day; test2-v5era is the frozen C3 test set; "
                        "edit by hand"}


def load_protect(daily=None):
    """`daily/protect.json`: days and parts that prune never touches (an active pre-registration). Seeded once."""
    p = Path(daily or DAILY) / "protect.json"
    if not p.exists():
        atomic_write(p, json.dumps(PROTECT_SEED, indent=1) + "\n")
    return json.loads(p.read_text())


def open_item_days(root):
    """Madrid days that have an open review item (window tables and uncertain live clips)."""
    import kivi_live_wakes as L
    return {L.local_day(it["row"]["utc"]) for it in L.queue_items(root)}


def prune_plan(root, today, older_days, protect, open_days):
    """Room-audio pieces of every `negatives*` set that is older than `older_days` (Madrid date of the piece start) and not
    protected. Returns (candidates, skipped Counter by reason). Candidate: {set, clip, path, day, bytes}."""
    import kivi_extract as X
    import kivi_real_common as C
    root = Path(root)
    cutoff = today - timedelta(days=older_days)
    cands, skipped = [], Counter()
    for man in sorted(root.glob("negatives*/manifest.jsonl")):
        for r in C.read_jsonl(man):
            path = root / r["clip"]
            if not path.is_file():
                continue
            day = datetime.fromtimestamp(X.parse_utc(r["start_utc"]).timestamp(), ZoneInfo(TZ)).date()
            if day > cutoff:
                skipped["newer than the cutoff"] += 1
            elif r.get("split") in protect.get("parts", ()):
                skipped["protected part"] += 1
            elif day.isoformat() in protect.get("days", ()):
                skipped["protected day"] += 1
            elif day.isoformat() in open_days:
                skipped["open review item that day"] += 1
            else:
                cands.append({"set": man.parent.name, "clip": r["clip"], "path": path, "day": day.isoformat(),
                              "bytes": path.stat().st_size})
    return cands, skipped


def cmd_prune(a, out=print, today=None):
    today = today or madrid_today()
    protect = load_protect()
    cands, skipped = prune_plan(ROOT, today, a.older_than_days, protect, open_item_days(ROOT))
    per_day = defaultdict(lambda: [0, 0])
    for c in cands:
        per_day[(c["day"], role_of(c["day"]))][0] += 1
        per_day[(c["day"], role_of(c["day"]))][1] += c["bytes"]
    out(f"raw room audio older than {a.older_than_days} days (before {today - timedelta(days=a.older_than_days)}), "
        f"protected days {protect.get('days')}, parts {protect.get('parts')}")
    for (d, role), (n, b) in sorted(per_day.items()):
        out(f"  {d} ({role}): {n} piece(s), {b / 1e9:.2f} GB")
    total = sum(c["bytes"] for c in cands)
    out(f"would delete {len(cands)} piece(s), {total / 1e9:.2f} GB; kept: " + (", ".join(f"{k} {v}" for k, v in skipped.items()) or "nothing"))
    out("labels, scores, queue tables, window clips and manifests are never deleted")
    if not a.yes:
        out("nothing deleted (list only); run again with --yes to delete exactly this list")
        return 0
    allowed = [Path(os.path.realpath(ROOT / c["set"])) for c in cands]
    done = freed = 0
    log = []
    for c, base in zip(cands, allowed):
        real = Path(os.path.realpath(c["path"]))
        if base not in real.parents:                       # never follow a link out of the set directory
            out(f"skipped (outside its set): {c['clip']}")
            continue
        os.unlink(c["path"])
        done += 1
        freed += c["bytes"]
        log.append({"clip": c["clip"], "day": c["day"], "bytes": c["bytes"],
                    "pruned_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    if log:
        DAILY.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(DAILY / "pruned.jsonl", "a", encoding="utf-8") as f:
            f.write("".join(json.dumps(x) + "\n" for x in log))
        os.chmod(DAILY / "pruned.jsonl", 0o600)
    out(f"deleted {done} piece(s), {freed / 1e9:.2f} GB; manifests keep their rows (pieces that are gone are skipped by the tools); "
        f"log {DAILY / 'pruned.jsonl'}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["status", "run", "prune", "_split", "_replay"])
    ap.add_argument("--day", help="one Europe/Madrid day YYYY-MM-DD")
    ap.add_argument("--from", dest="from_day", help="(run, first time) first day of the loop")
    ap.add_argument("--redo", action="store_true", help="(run --day) run a done day again")
    ap.add_argument("--older-than-days", type=int, default=PRUNE_DAYS, help="(prune) default 45")
    ap.add_argument("--yes", action="store_true", help="(prune) delete the listed pieces; without it nothing is deleted")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-heavy", type=int, default=2, help="heavy jobs allowed in total, this one included")
    ap.add_argument("--wait-min", type=float, default=0.0, help="wait this long for heavy jobs to end (poll 5 min), else exit 4")
    ap.add_argument("--jobs", type=int, default=6, help="replay workers (at most 8)")
    ap.add_argument("--miss-limit", type=int, default=MISS_LIMIT)
    ap.add_argument("--context", default="grigri")
    ap.add_argument("--namespace", default="home-assistant")
    ap.add_argument("--pod", default="home-assistant-hass-0")
    ap.add_argument("--container")
    ap.add_argument("--recordings-root", default="/voice-recordings")
    ap.add_argument("--from-dir")
    ap.add_argument("--devices")
    ap.add_argument("--devices-file")
    ap.add_argument("--live", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    a.jobs = min(8, max(1, a.jobs))
    if a.day:
        date.fromisoformat(a.day)
    if a.what == "status":
        cmd_status(a)
    elif a.what == "run":
        sys.exit(cmd_run(a))
    elif a.what == "prune":
        cmd_prune(a)
    elif a.what == "_split":
        cmd_split(a)
    else:
        cmd_replay(a)


if __name__ == "__main__":
    main()
