#!/usr/bin/env python3
"""Owner-run: the live wake detections of the satellites ("oye Kivi" era), cut out of Home Assistant's
debug recordings so the owner can label them by listening.

    kivi_live_wakes.py list                 # detection runs in scope, counts per device / model / bucket (read only)
    kivi_live_wakes.py extract              # copy the clips and write live_wakes.csv
    kivi_live_wakes.py review               # YOU listen and label (plays audio; nobody else runs this)
    kivi_live_wakes.py split [--apply]      # after labelling: train / test by local day, counts only

What a detection is. The satellite's wake file (`00_wake-*.wav`) is the continuous stream from the start
of a run to the detection; the wake word sits in its last 1-3 s. A wake file also closes on an idle gap,
a pipeline update, a Home Assistant restart or a mute, so a detection needs a `01_stt-*.wav` in the same run
directory (or an stt-only run that starts within 3 s of the wake file end). Closed wake files without one are
only counted. The run directory is `time.monotonic_ns()`; run start = node boot + ns (checked against the file
times, which are used when they disagree by more than 5 s).

Scope: detections that ended at or after `--scope-start` (2026-11-07T19:59:21Z, the first "oye Kivi" model),
with the model of each era (see `ERAS`), minus the windows (+-120 s) of every recorded session of the dataset.

Output, under `<dataset>/live/` (mode 700/600, never in git):
    <device>/<run_start_utc>/wake_tail.wav   the last 6 s of the wake file (the whole file if shorter)
    <device>/<run_start_utc>/stt.wav         the whole 01_stt file of the run
    live_wakes.csv                           one row per run; label, speaker and split are yours
`extract` is idempotent: existing files are kept, and a row that already exists (with its label) is never changed.
The tool never prints audio content: counts, durations, times and levels only.
"""
import argparse
import csv
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kivi_extract as X  # noqa: E402
import kivi_real_common as C  # noqa: E402

RATE = C.RATE
BYTES_PER_S = 2 * RATE
TAIL_S = 6.0
SCOPE_START = "2026-11-07T19:59:21Z"
BOOT_UTC = "2026-11-03T22:08:20Z"          # node boot: run start = boot + monotonic ns of the run directory
BOOT_TOL_S = 5.0                           # beyond this the file times win over boot + ns
STT_PAIR_S = (-1.0, 3.0)                   # an stt-only run counts for a wake file that starts this far from its end
CLOSED_AGE_S = 120.0
SHORT_STT_S = 1.5                          # shorter: nothing was said, suspect false wake
CAP_STT_S = 14.5                           # the speech-to-text stage stops at about 15 s: a run that hit it is suspect
SESSION_MARGIN_S = 120.0
TZ = "Europe/Madrid"
LABELS = ("true", "false", "unclear")
SPEAKERS = ("spk01", "spk02", "spk-other", "unknown")
BUCKETS = ("suspect-false", "suspect-true")
TEST_SHARE = 0.30

# model id per era: (model, from, until, devices or None for all). The first matching entry wins; times are UTC.
# oye_kivi (v4) from 2026-11-07 19:59:21Z; c3-a ran oye_kivi_next in its pipeline "oye Kivi" from 2026-11-08 11:56:45Z,
# both TVs run oye_kivi_next since 2026-11-08 13:28:10Z.
ERAS = (
    ("oye_kivi_next", "2026-11-08T11:56:45Z", None, ("c3-a",)),
    ("oye_kivi_next", "2026-11-08T13:28:10Z", None, None),
    ("oye_kivi", SCOPE_START, "2026-11-08T13:28:10Z", None),
)

FIELDS = ("run_id", "device", "model", "utc", "wake_len", "stt_len", "bucket", "label", "speaker", "split",
          "sha256", "sha256_stt", "pipeline", "stt_run")


# ---------------------------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------------------------


def epoch(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def dir_stamp(t):
    """Directory name of a run start: 20261108T123456.789Z."""
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%dT%H%M%S.%f")[:-3] + "Z"


def model_of(device, t):
    for model, lo, hi, devs in ERAS:
        if (devs is None or device in devs) and epoch(lo) <= t and (hi is None or t < epoch(hi)):
            return model
    return None


def session_windows(root, margin_s=SESSION_MARGIN_S):
    """{device: [(t0, t1)]}: every recorded session of the dataset (`*/session.json`) with a margin."""
    out = {}
    for sj in Path(root).glob("*/session.json"):
        try:
            s = json.loads(sj.read_text(encoding="utf-8"))
            t0 = X.parse_utc(s["t0_utc"]).timestamp()
            t1 = X.parse_utc(s["end_utc"]).timestamp() if s.get("end_utc") else t0 + float(s.get("end_offset_s") or 0)
            if s.get("kind") == "button-takes":
                t0, t1 = X.parse_utc(s["since"]).timestamp(), X.parse_utc(s["until"]).timestamp()
            out.setdefault(s.get("device"), []).append((t0 - margin_s, t1 + margin_s))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------------------------


def run_start(f, boot, warn):
    """Start of a run: boot + the monotonic ns of its directory, unless the file times disagree."""
    by_files = f["start"] if f.get("start") is not None else f["mtime"]
    try:
        by_dir = boot + int(f["run"]) / 1e9
    except ValueError:
        warn["non-numeric run dir"] += 1
        return by_files
    if abs(by_dir - by_files) > BOOT_TOL_S:
        warn["run dir time differs from file times"] += 1
        return by_files
    return by_dir


def find_runs(fetcher, devices_prefix, *, now, boot=None, scope_start=SCOPE_START, windows=None, session_root=None):
    """Detection runs and counts.

    Returns {"runs": [dict], "no_detection": Counter[(device, model)], "open": int, "stt_only": int,
    "out_of_scope": int, "in_session": int, "warnings": Counter}. A run dict has device, model, pipeline, run, start,
    wake_end, wake (the listed wake file), stt (the listed stt file), stt_run (None when in the same directory)."""
    boot = epoch(BOOT_UTC) if boot is None else boot
    s0 = epoch(scope_start)
    windows = session_windows(session_root) if windows is None and session_root else (windows or {})
    warn = Counter()
    res = {"runs": [], "no_detection": Counter(), "open": 0, "stt_only": 0, "out_of_scope": 0, "in_session": 0,
           "warnings": warn}
    for dev, prefix in devices_prefix.items():
        wakes = X.list_files(fetcher, prefix, "wake")
        stts = X.list_files(fetcher, prefix, "stt")
        by_dir = {(f["pipeline"], f["run"]): f for f in stts}
        used = set()
        wake_dirs = {(f["pipeline"], f["run"]) for f in wakes}
        stt_only = [f for f in stts if (f["pipeline"], f["run"]) not in wake_dirs]
        for w in sorted(wakes, key=lambda f: f["mtime"]):
            if now - w["mtime"] < CLOSED_AGE_S:
                res["open"] += 1
                continue
            end = w["mtime"]
            if end < s0:
                res["out_of_scope"] += 1
                continue
            stt, stt_run = by_dir.get((w["pipeline"], w["run"])), None
            if stt is not None:
                used.add((stt["pipeline"], stt["run"]))
            else:
                near = [(abs(run_start(o, boot, warn) - end), o) for o in stt_only
                        if (o["pipeline"], o["run"]) not in used
                        and STT_PAIR_S[0] <= run_start(o, boot, warn) - end <= STT_PAIR_S[1]]
                if near:
                    stt = min(near, key=lambda x: x[0])[1]
                    used.add((stt["pipeline"], stt["run"]))
                    stt_run = f"{stt['pipeline']}/{stt['run']}"
            model = model_of(dev, end)
            start = run_start(w, boot, warn)
            tail0 = max(start, end - TAIL_S)
            stt_end = stt["mtime"] if stt else end
            if any(b > tail0 and a < stt_end for a, b in windows.get(dev, ())):
                res["in_session"] += 1
                continue
            if stt is None:
                res["no_detection"][(dev, model)] += 1
                continue
            if now - stt["mtime"] < CLOSED_AGE_S:
                res["open"] += 1
                continue
            res["runs"].append({"device": dev, "model": model, "pipeline": w["pipeline"], "run": w["run"],
                                "start": start, "wake_end": end, "wake": w, "stt": stt, "stt_run": stt_run})
        res["stt_only"] += sum(1 for o in stt_only if (o["pipeline"], o["run"]) not in used
                               and now - o["mtime"] >= CLOSED_AGE_S and o["mtime"] >= s0)
    res["runs"].sort(key=lambda r: (r["start"], r["device"]))
    return res


def bucket_of(stt_len):
    return "suspect-false" if (stt_len < SHORT_STT_S or stt_len >= CAP_STT_S) else "suspect-true"


def run_id(r):
    return f"{r['pipeline']}/{r['run']}"


def run_row(r):
    return {"run_id": run_id(r), "device": r["device"], "model": r["model"], "utc": dir_stamp(r["start"]),
            "wake_len": f"{r['wake']['dur']:.2f}", "stt_len": f"{r['stt']['dur']:.2f}",
            "bucket": bucket_of(r["stt"]["dur"]), "pipeline": r["pipeline"], "stt_run": r["stt_run"] or ""}


def devices_prefix(a):
    devs = [d.strip() for d in a.devices.split(",")] if a.devices else list(C.load_devices(a.devices_file))
    return {d: C.device_prefix(d, a.devices_file) for d in devs}


def get_fetcher(a):
    if getattr(a, "from_dir", None):
        return X.LocalFetcher(a.from_dir)
    return X.KubectlFetcher(a.context, a.namespace, a.pod, a.container, a.recordings_root)


def scan(a, fetcher, now=None):
    root = Path(a.dataset_root).expanduser()
    return find_runs(fetcher, devices_prefix(a), now=(now if now is not None else (a.now if a.now is not None else
                     datetime.now(timezone.utc).timestamp())), scope_start=a.scope_start, session_root=root)


# ---------------------------------------------------------------------------------------------
# csv table
# ---------------------------------------------------------------------------------------------


def read_table(path):
    p = Path(path)
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return [{k: (row.get(k) or "") for k in FIELDS} for row in csv.DictReader(f)]


def write_table(path, rows):
    """Atomic rewrite (temp file, fsync, rename), mode 600: a crash leaves the old table whole."""
    p = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------------------------


def cmd_list(a, fetcher=None, out=print):
    res = scan(a, fetcher or get_fetcher(a))
    runs = res["runs"]
    out(f"{'device':6} {'model':14} {'run start UTC':25} {'wake s':>7} {'stt s':>6}  bucket")
    for r in runs:
        row = run_row(r)
        out(f"{r['device']:6} {str(r['model']):14} {row['utc']:25} {row['wake_len']:>7} {row['stt_len']:>6}  {row['bucket']}"
            + (f"  (stt in {row['stt_run']})" if row["stt_run"] else ""))
    out("")
    cnt = Counter((r["device"], r["model"], run_row(r)["bucket"]) for r in runs)
    out("detection runs per device / model / bucket:")
    for (d, m, b), n in sorted(cnt.items(), key=lambda kv: tuple(map(str, kv[0]))):
        out(f"  {d} {m} {b}: {n}")
    out(f"  total: {len(runs)}")
    out("closed wake files without a detection (not extracted), per device / model:")
    for (d, m), n in sorted(res["no_detection"].items(), key=lambda kv: tuple(map(str, kv[0]))):
        out(f"  {d} {m}: {n}")
    if not res["no_detection"]:
        out("  none")
    out(f"left out: {res['in_session']} run(s) in a recorded session window, {res['open']} file(s) still open, "
        f"{res['stt_only']} stt-only run(s) in scope not tied to a wake file (continued conversations)")
    for k, n in res["warnings"].items():
        out(f"warning: {k}: {n}")
    return res


# ---------------------------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------------------------


def copy_wav(fetcher, path, first_sample, last_sample, dst):
    """Samples [first, last) of a listed file as a new WAV at `dst`, through `.part`, byte count checked. False if it exists."""
    dst = Path(dst)
    if dst.exists():
        return False
    b0, b1 = 44 + 2 * first_sample, 44 + 2 * last_sample
    w = C.PartWavWriter(dst)
    try:
        for chunk in fetcher.fetch_range(path, b0, b1):
            w.write(chunk)
        w.commit(expected_bytes=b1 - b0)
    except BaseException as e:
        w.close()
        raise SystemExit(f"copy for {dst.parent.name}/{dst.name} failed ({e}); the partial file {w.part.name} was left")
    return True


def cmd_extract(a, fetcher=None, out=print):
    fetcher = fetcher or get_fetcher(a)
    res = scan(a, fetcher)
    root = Path(a.dataset_root).expanduser()
    live = C.private_mkdir(root / "live", root)
    table_path = live / "live_wakes.csv"
    rows = read_table(table_path)
    have = {r["run_id"]: r for r in rows}
    copied = kept = 0
    for r in res["runs"]:
        row = run_row(r)
        d = C.private_mkdir(live / r["device"] / row["utc"], root)
        n = r["wake"]["n"]
        n_tail = min(n, int(TAIL_S * RATE))
        fresh = copy_wav(fetcher, r["wake"]["path"], n - n_tail, n, d / "wake_tail.wav")
        fresh_s = copy_wav(fetcher, r["stt"]["path"], 0, r["stt"]["n"], d / "stt.wav")
        copied += fresh + fresh_s
        kept += (not fresh) + (not fresh_s)
        cur = have.get(row["run_id"])
        if cur is None:
            row["sha256"], row["sha256_stt"] = sha256_file(d / "wake_tail.wav"), sha256_file(d / "stt.wav")
            rows.append({k: row.get(k, "") for k in FIELDS})
            have[row["run_id"]] = rows[-1]
        else:                                    # never touches label, speaker, split
            for k in ("device", "model", "utc", "wake_len", "stt_len", "bucket", "pipeline", "stt_run"):
                if not cur[k]:
                    cur[k] = row[k]
            for k, name in (("sha256", "wake_tail.wav"), ("sha256_stt", "stt.wav")):
                if not cur[k]:
                    cur[k] = sha256_file(d / name)
    rows.sort(key=lambda x: (x["utc"], x["device"]))
    write_table(table_path, rows)
    out(f"{len(res['runs'])} detection run(s) in scope; {copied} file(s) copied, {kept} already there; table {table_path} "
        f"({len(rows)} row(s))")
    return res


# ---------------------------------------------------------------------------------------------
# review (owner only)
# ---------------------------------------------------------------------------------------------


def detect_player():
    if shutil.which("pw-play"):
        return ["pw-play"]
    if shutil.which("aplay"):
        return ["aplay", "-q"]
    raise SystemExit("no player found: install pipewire (pw-play) or alsa-utils (aplay), or pass --player 'cmd'")


def make_player(spec=None):
    """A function play(path) that blocks until the file was played."""
    cmd = shlex.split(spec) if spec else detect_player()

    def play(path):
        subprocess.run(cmd + [str(path)], check=False)
    return play


def review_order(rows):
    """Unlabelled rows, suspect-false first, then by time."""
    todo = [r for r in rows if not r["label"]]
    return sorted(todo, key=lambda r: (r["bucket"] != "suspect-false", r["utc"], r["device"]))


def ask(prompt, valid, input_fn):
    while True:
        s = input_fn(prompt).strip().lower()
        if s in valid:
            return s


def cmd_review(a, player=None, input_fn=input, out=print):
    root = Path(a.dataset_root).expanduser()
    live = root / "live"
    table_path = live / "live_wakes.csv"
    rows = read_table(table_path)
    if not rows:
        raise SystemExit(f"no table at {table_path}: run extract first")
    play = player or make_player(a.player)
    todo = review_order(rows)
    if a.again:
        todo = sorted(rows, key=lambda r: (r["bucket"] != "suspect-false", r["utc"], r["device"]))
    out(f"{len(todo)} clip(s) to label, {len(rows) - len([r for r in rows if not r['label']])} already labelled. "
        "For each clip you hear the wake tail, then the request audio.")
    out("keys: t = true wake, f = false wake, u = unclear, r = replay, s = skip, q = quit (progress is saved after every clip)")
    done = 0
    try:
        for i, r in enumerate(todo, 1):
            d = live / r["device"] / r["utc"]
            out(f"\n[{i}/{len(todo)}] {r['device']} {r['model']} {r['utc']}  wake {r['wake_len']} s, stt {r['stt_len']} s, {r['bucket']}")
            while True:
                play(d / "wake_tail.wav")
                play(d / "stt.wav")
                k = ask("label [t/f/u], r = replay, s = skip, q = quit: ", ("t", "f", "u", "r", "s", "q"), input_fn)
                if k != "r":
                    break
            if k == "q":
                break
            if k == "s":
                continue
            label = {"t": "true", "f": "false", "u": "unclear"}[k]
            keys = {"1": "spk01", "2": "spk02", "3": "spk-other", "4": "unknown"}
            sp = ask("speaker [1 = spk01, 2 = spk02, 3 = spk-other, 4 = unknown]: ", tuple(keys), input_fn)
            r["label"], r["speaker"] = label, keys[sp]
            write_table(table_path, rows)
            done += 1
    except (KeyboardInterrupt, EOFError):
        out("\ninterrupted")
    out(f"labelled {done} clip(s) in this run; table saved ({table_path})")
    return done


# ---------------------------------------------------------------------------------------------
# split
# ---------------------------------------------------------------------------------------------


def local_day(utc_stamp):
    from zoneinfo import ZoneInfo
    t = datetime.strptime(utc_stamp, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
    return t.astimezone(ZoneInfo(TZ)).date().isoformat()


def propose_split(rows, share=TEST_SHARE):
    """Returns ({run_id: split}, {speaker: info}). Per speaker the most recent local days (Europe/Madrid) that
    together hold at least `share` of that speaker's true wakes are `test`, the other days `train`. A speaker whose
    true wakes fall on one day only (everything would be test) stays unassigned. Rows labelled false get `test` on
    or after the earliest test day of any speaker, `train` before it; unclear rows stay unassigned."""
    assign, info = {}, {}
    true_rows = [r for r in rows if r["label"] == "true"]
    for sp in sorted({r["speaker"] or "unknown" for r in true_rows}):
        mine = [r for r in true_rows if (r["speaker"] or "unknown") == sp]
        per_day = Counter(local_day(r["utc"]) for r in mine)
        days = sorted(per_day, reverse=True)
        need, got, test_days = share * len(mine), 0, []
        for dd in days:
            if got >= need:
                break
            test_days.append(dd)
            got += per_day[dd]
        if len(test_days) == len(days):
            info[sp] = {"true": len(mine), "days": len(days), "test_days": [], "note": "single day or too few: left unassigned"}
            continue
        for r in mine:
            assign[r["run_id"]] = "test" if local_day(r["utc"]) in test_days else "train"
        info[sp] = {"true": len(mine), "days": len(days), "test_days": sorted(test_days),
                    "test": sum(per_day[dd] for dd in test_days), "train": len(mine) - sum(per_day[dd] for dd in test_days)}
    test_starts = [min(i["test_days"]) for i in info.values() if i["test_days"]]
    if test_starts:
        cut = min(test_starts)
        for r in rows:
            if r["label"] == "false":
                assign[r["run_id"]] = "test" if local_day(r["utc"]) >= cut else "train"
    return assign, info


def cmd_split(a, out=print):
    root = Path(a.dataset_root).expanduser()
    table_path = root / "live" / "live_wakes.csv"
    rows = read_table(table_path)
    if not rows:
        raise SystemExit(f"no table at {table_path}: run extract first")
    lab = Counter(r["label"] or "unlabelled" for r in rows)
    out("labels: " + ", ".join(f"{k} {v}" for k, v in sorted(lab.items())))
    if not lab.get("true"):
        out("no true wakes labelled yet: nothing to split")
        return {}
    assign, info = propose_split(rows, a.test_share)
    for sp, i in info.items():
        out(f"  {sp}: {i['true']} true wake(s) on {i['days']} local day(s) ({TZ}); "
            + (f"test = {len(i['test_days'])} most recent day(s), {i['test']} true wake(s) "
               f"({i['test'] / i['true']:.0%}); train {i['train']}" if i["test_days"] else i["note"]))
    fcnt = Counter(assign[r["run_id"]] for r in rows if r["label"] == "false" and r["run_id"] in assign)
    out("  rows labelled false: " + (", ".join(f"{s} {n}" for s, n in sorted(fcnt.items())) or "none"))
    out(f"proposal covers {len(assign)} of {len(rows)} row(s)")
    if a.apply:
        for r in rows:
            if r["run_id"] in assign:
                r["split"] = assign[r["run_id"]]
        write_table(table_path, rows)
        out("applied to the split column")
    else:
        out("proposal only; --apply writes it into the table")
    return assign


# ---------------------------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["list", "extract", "review", "split"])
    ap.add_argument("--dataset-root", default=C.DEFAULT_DATASET_ROOT)
    ap.add_argument("--devices", help="comma list (default: every device in kivi_devices.json)")
    ap.add_argument("--devices-file")
    ap.add_argument("--context", default="grigri")
    ap.add_argument("--namespace", default="home-assistant")
    ap.add_argument("--pod", default="home-assistant-hass-0")
    ap.add_argument("--container")
    ap.add_argument("--recordings-root", default="/voice-recordings")
    ap.add_argument("--from-dir", help="local copy laid out like /voice-recordings")
    ap.add_argument("--scope-start", default=SCOPE_START)
    ap.add_argument("--now", type=float, help=argparse.SUPPRESS)
    ap.add_argument("--player", help="(review) player command, the file is appended; default pw-play, else aplay -q")
    ap.add_argument("--again", action="store_true", help="(review) go through labelled clips too")
    ap.add_argument("--apply", action="store_true", help="(split) write the proposal into the split column")
    ap.add_argument("--test-share", type=float, default=TEST_SHARE, help="(split) share of true wakes in test, default 0.30")
    a = ap.parse_args(argv)
    {"list": cmd_list, "extract": cmd_extract, "review": cmd_review, "split": cmd_split}[a.what](a)


if __name__ == "__main__":
    main()
