#!/usr/bin/env python3
"""Owner-run: the live wake detections of the satellites ("oye Kivi" era), cut out of Home Assistant's
debug recordings so the owner can label them by listening.

    kivi_live_wakes.py list                 # detection runs in scope, counts per device / model / bucket (read only)
    kivi_live_wakes.py extract              # copy the clips and write live_wakes.csv
    kivi_live_wakes.py review               # YOU listen and label (plays audio; nobody else runs this)
    kivi_live_wakes.py review --table miss_candidates.csv   # the same for the missed-wake candidates (see below)
    kivi_live_wakes.py queue                # YOU label every pending item of every table, in priority order (see below)
    kivi_live_wakes.py autolabel [--dry-run] # local auto-labeller (kivi_autolabel.py): fills EMPTY label/speaker cells
    kivi_live_wakes.py autolabel --trigger-only [--dry-run]   # only trigger / peak_t_s / peak_raw / model evidence (exact replay)
    kivi_live_wakes.py fix-models [--dry-run]                 # model column from ERAS (detection time); then --trigger-only
    kivi_live_wakes.py split [--apply]      # after labelling: train / test by local day, counts only
    kivi_live_wakes.py import-button-labels [--dry-run]   # false-wake button presses become `false` labels (see below)

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
The `trigger` column (wake | button | unknown) says whether the run was started by a wake-word detection or by something
else, in practice the remote's mic button: the live model is replayed on the wake tail and must reach its live
threshold in the last 2 s (kivi_autolabel.trigger_of). Button runs pair with an stt-only run, like a detection.
The tool never prints audio content: counts, durations, times and levels only.

Missed-wake candidates: `kivi_replay_mine.py candidates` cuts 6 s windows (`<dataset>/live-misses/<part>/<device>/<utc>/
window.wav`) around replay peaks of the v5-era room audio where the wake word model did not fire, and writes
`<dataset>/live-misses/miss_candidates.csv`. `review --table miss_candidates.csv` (a bare name is looked up in
`live-misses/`) plays each window, best rank first, without showing the scores, and stores your label
(attempt = someone said oye Kivi / Kivi to it, other-speech, tv-media, noise, unclear) and, for attempt and other-speech,
the speaker. The live-wake review above is unchanged.

Labelling queue (`queue`): one command that walks every pending item of every review table, scores hidden, in this order:
(0) the blind autolabel validation sample (`live/autolabel_validation.csv`, built by `kivi_false_wake_audit.py sample`:
about 40 live clips, the auto label never shown; the label is t true / f false + cause o other speech, m tv / media,
n noise / n not a wake / u unclear, plus the speaker; it is carried into `live_wakes.csv` as an owner label, the auto
label stays only in the frozen key that `kivi_false_wake_audit.py analyze` reads),
(1) replay false wakes of the live model (`live-misses/daily_false_wakes.csv`, written by `kivi_collect.py`),
(2) uncertain live clips (`live/live_wakes.csv`: unlabelled, auto unclear (first), auto not-a-wake (button clips), auto true
with an unknown speaker or low confidence; auto true and false are trusted; an owner label is never offered again; a clip waiting in the validation sample is not offered twice),
(3) missed-wake candidates (every other `live-misses/*.csv`, best rank first). Each label is saved at once (a fresh read of the table, so a collector writing meanwhile loses nothing); `q`
or Ctrl-C stops at any time, a skipped item comes back next time. It prints the items left per table at start and end.
`--minutes N` and `--max-items N` end a session by themselves. `--day D` / `--device X` narrow the queue. Plays audio: run it
in your own terminal, never from an agent.
False-wake button (`import-button-labels`): the owner presses `input_button.kivi_false_wake_<device>` after a wake that
was a mistake. The press times are read like `kivi_extract.py --button-takes` reads `kivi_take` (one read-only SELECT of the
recorder through `kubectl exec ... psql`, or `--presses-file` with `{"c3-a": [ISO times]}`). A press labels the run of the
same TV whose DETECTION (the end of the wake file, not its start: a run can be open for hours before the wake) ended 0-60 s
(`--window-s`) before it. The clip is extracted when missing and labelled `false` with label_source `owner-button`;
an existing owner label is never changed (a conflict is reported), a press with no run or with several runs is only
reported. Counts and times are printed, never audio.
`extract` and `autolabel` take `--day YYYY-MM-DD` (one Europe/Madrid day, by run start) for the daily loop.
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
from collections import Counter
from datetime import datetime, timezone
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
LABELS = ("true", "false", "unclear", "not-a-wake")
SPEAKERS = ("spk01", "spk02", "spk-other", "unknown")
BUCKETS = ("suspect-false", "suspect-true")
TEST_SHARE = 0.30

# model id per era: (model, from, until, devices or None for all). The first matching entry wins; times are UTC and are
# compared with the detection time (the end of the wake file). oye_kivi (v4) from 2026-11-07 19:59:21Z; c3-a ran
# oye_kivi_next in its pipeline "oye Kivi" from 2026-11-08 11:56:45Z, both TVs oye_kivi_next since 2026-11-08 13:28:10Z;
# oye_kivi_ctx (v6) 2026-11-11 22:11Z (promote) to about 22:40Z (owner rollback in HA; the driver recorded the demote at
# 22:42:27Z); oye_kivi_next again until the v6.2 promote wrote the household pipeline at 07:40:55Z on 2026-11-12
# (`promotion.promoted_at` of the release state; HA ends open runs on that update; the promote step itself finished at
# 07:43:05Z after its run checks); oye_kivi_ctx2 (v6.2) since. Both TVs use the household pipeline "Kiwi".
V6_PROMOTE, V6_ROLLBACK, V62_PROMOTE = "2026-11-11T22:11:00Z", "2026-11-11T22:40:00Z", "2026-11-12T07:40:55Z"
ERAS = (
    ("oye_kivi_ctx2", V62_PROMOTE, None, None),
    ("oye_kivi_ctx", V6_PROMOTE, V6_ROLLBACK, None),
    ("oye_kivi_next", "2026-11-08T11:56:45Z", V62_PROMOTE, ("c3-a",)),
    ("oye_kivi_next", "2026-11-08T13:28:10Z", V62_PROMOTE, None),
    ("oye_kivi", SCOPE_START, "2026-11-08T13:28:10Z", None),
)

MISS_LABELS = {"a": "attempt", "o": "other-speech", "t": "tv-media", "n": "noise", "u": "unclear"}   # miss-candidate review
MISS_FIELDS = ("id", "device", "part", "utc", "peak_v1", "peak_v4", "peak_v5", "retry", "retry_detail", "rank",
               "label", "speaker", "level_dbfs", "peak_dbfs", "clip")

FIELDS = ("run_id", "device", "model", "utc", "wake_len", "stt_len", "bucket", "label", "speaker", "split",
          "sha256", "sha256_stt", "pipeline", "stt_run", "trigger", "label_source", "label_conf", "speaker_conf",
          "auto_evidence", "phrase_end_s", "continuous_command", "phrase_in_stt", "stt_phrase_start_s",
          "stt_phrase_end_s", "peak_t_s", "peak_raw")


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


def write_csv_atomic(path, fields, rows):
    """Atomic rewrite (temp file, fsync, rename), mode 600: a crash leaves the old table whole."""
    p = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
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


def write_table(path, rows):
    write_csv_atomic(path, FIELDS, rows)


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
    sep = sum(1 for r in runs if r["stt_run"])
    out(f"request audio in the wake run's own directory: {len(runs) - sep}; from a separate stt-only run: {sep} "
        "(button-suspect; the decisive check, the live model's score on the tail, runs in `extract`)")
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


def make_trigger_scorer(a):
    """The server-exact model scorer, or None when its libraries or model files are missing (trigger stays unknown)."""
    try:
        import kivi_autolabel as A
        return A.ModelScorer(getattr(a, "work", None) or str(C.kivi_home()))
    except Exception as e:                                       # noqa: BLE001
        print(f"warning: trigger classification unavailable ({type(e).__name__}); trigger stays empty")
        return None


def trigger_for(row, tail_path, scorer):
    import kivi_autolabel as A
    if scorer is None:
        return "unknown"
    pcm = C.read_pcm16(tail_path)
    return A.trigger_of(row["model"], row["stt_run"], scorer.peaks(pcm, A.wake_total(row, len(pcm))), len(pcm) / RATE)[0]


def cmd_extract(a, fetcher=None, out=print, scorer=False, only=None):
    """`only`: a set of run ids; just those runs are extracted."""
    fetcher = fetcher or get_fetcher(a)
    if scorer is False:
        scorer = make_trigger_scorer(a)
    res = scan(a, fetcher)
    if only is not None:
        res = dict(res, runs=[r for r in res["runs"] if run_id(r) in only])
    if getattr(a, "day", None):                  # one Europe/Madrid day, by run start (the day of the row's `utc`)
        res = dict(res, runs=[r for r in res["runs"] if local_day(dir_stamp(r["start"])) == a.day])
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
            row["trigger"] = trigger_for(row, d / "wake_tail.wav", scorer) if scorer is not None else ""
            rows.append({k: row.get(k, "") for k in FIELDS})
            have[row["run_id"]] = rows[-1]
        else:                                    # never touches label, speaker, split
            for k in ("device", "model", "utc", "wake_len", "stt_len", "bucket", "pipeline", "stt_run"):
                if not cur[k]:
                    cur[k] = row[k]
            for k, name in (("sha256", "wake_tail.wav"), ("sha256_stt", "stt.wav")):
                if not cur[k]:
                    cur[k] = sha256_file(d / name)
            if not cur["trigger"] and scorer is not None:
                cur["trigger"] = trigger_for(cur, d / "wake_tail.wav", scorer)
    rows.sort(key=lambda x: (x["utc"], x["device"]))
    write_table(table_path, rows)
    out(f"{len(res['runs'])} detection run(s) in scope; {copied} file(s) copied, {kept} already there; table {table_path} "
        f"({len(rows)} row(s))")
    return res


# ---------------------------------------------------------------------------------------------
# false-wake button presses -> owner labels
# ---------------------------------------------------------------------------------------------

BUTTON_SOURCE = "owner-button"
BUTTON_SINCE = "2026-11-11T17:30:00Z"      # the helper was deployed
BUTTON_WINDOW_S = 60.0                     # the press comes this long after the end of the wake file at most
BUTTON_TOL_S = 2.0                         # ... and at most this much before it (clock rounding)


def button_entity(device):
    return "input_button.kivi_false_wake_" + device.replace("-", "_")


def match_presses(runs, presses, window_s=BUTTON_WINDOW_S, tol_s=BUTTON_TOL_S):
    """[(device, press_epoch, [runs])] for every press, in time order: the runs of that device whose detection
    (end of the wake file) is `tol_s` after to `window_s` before the press. A run is not a candidate when it was
    still open at the press (its detection came later)."""
    out = []
    for dev, times in presses.items():
        for t in times:
            cand = [r for r in runs if r["device"] == dev and -tol_s <= t - r["wake_end"] <= window_s]
            out.append((dev, t, cand))
    return sorted(out, key=lambda x: (x[1], x[0]))


def apply_button_labels(table_path, run_ids):
    """Write `false` / owner-button into a FRESH read of the table. An owner label (non-empty, label_source not
    auto-*) is kept. Returns (set, kept_false, conflicts, missing) as lists of run ids."""
    fresh = read_table(table_path)
    by = {r["run_id"]: r for r in fresh}
    new, kept, conflicts, missing = [], [], [], []
    for rid in run_ids:
        r = by.get(rid)
        if r is None:
            missing.append(rid)
        elif r["label"] and not r["label_source"].startswith("auto"):
            (kept if r["label"] == "false" else conflicts).append(rid)
        else:
            r["label"], r["label_source"], r["label_conf"] = "false", BUTTON_SOURCE, "1.00"
            new.append(rid)
    if new:
        write_table(table_path, fresh)
    return new, kept, conflicts, missing


def read_button_presses(a, since, until):
    """{device: [press epoch]} from --presses-file, else the recorder (read only SELECT, as kivi_extract does)."""
    devs = [d.strip() for d in a.devices.split(",")] if a.devices else list(C.load_devices(a.devices_file))
    if getattr(a, "presses_file", None):
        raw = json.loads(Path(a.presses_file).read_text(encoding="utf-8"))
        return {d: sorted(t for t in (X.parse_when(x) for x in raw.get(d, ())) if since <= t <= until) for d in devs}
    return {d: X.PostgresPresses(a.context, a.namespace, a.pg_pod, a.pg_container, a.pg_user, a.pg_db,
                                 entity=button_entity(d)).presses(since, until) for d in devs}


def cmd_import_button_labels(a, fetcher=None, out=print, presses=None):
    fetcher = fetcher or get_fetcher(a)
    now = a.now if a.now is not None else datetime.now(timezone.utc).timestamp()
    since = X.parse_when(getattr(a, "since", None) or BUTTON_SINCE)
    until = X.parse_when(a.until) if getattr(a, "until", None) else now
    presses = presses if presses is not None else read_button_presses(a, since, until)
    window_s = getattr(a, "window_s", None) or BUTTON_WINDOW_S
    res = scan(a, fetcher, now=now)
    matches = match_presses(res["runs"], presses, window_s)
    out(f"{sum(len(v) for v in presses.values())} press(es) between {dir_stamp(since)} and {dir_stamp(until)}: "
        + ", ".join(f"{d} {len(v)}" for d, v in sorted(presses.items())))
    wanted, unmatched, ambiguous = [], [], []
    for dev, t, cand in matches:
        stamp = dir_stamp(t)
        if len(cand) == 1:
            r = cand[0]
            out(f"  {stamp} {dev}: run {dir_stamp(r['start'])} ({run_id(r)}), detection {t - r['wake_end']:.1f} s before the press")
            wanted.append(run_id(r))
        elif not cand:
            out(f"  {stamp} {dev}: no run with a detection ended 0-{window_s:.0f} s before it; left alone")
            unmatched.append((dev, t))
        else:
            out(f"  {stamp} {dev}: {len(cand)} runs match ({', '.join(dir_stamp(r['start']) for r in cand)}); left alone")
            ambiguous.append((dev, t))
    ids = sorted(set(wanted))
    out(f"{len(wanted)} press(es) matched one run each ({len(ids)} distinct run(s)); {len(unmatched)} without a run, "
        f"{len(ambiguous)} ambiguous")
    dry = getattr(a, "dry_run", False)
    if dry or not ids:
        out("dry run: nothing written" if dry else "nothing to label")
        return {"matched": ids, "unmatched": unmatched, "ambiguous": ambiguous, "new": [], "kept": [], "conflicts": []}
    cmd_extract(a, fetcher, out=out, scorer=None, only=set(ids))      # trigger stays empty: the daily collect fills it
    table = Path(a.dataset_root).expanduser() / "live" / "live_wakes.csv"
    new, kept, conflicts, missing = apply_button_labels(table, ids)
    out(f"labelled false (owner-button): {len(new)}; already an owner label false: {len(kept)}; "
        f"owner label CONFLICTS kept: {len(conflicts)}; not in the table: {len(missing)}")
    for rid in conflicts:
        out(f"  conflict: {rid} is labelled {[r for r in read_table(table) if r['run_id'] == rid][0]['label']} by the owner; "
            "kept, the press disagrees")
    return {"matched": ids, "unmatched": unmatched, "ambiguous": ambiguous, "new": new, "kept": kept,
            "conflicts": conflicts, "missing": missing}


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


def review_order(rows, only_uncertain=False):
    """Unlabelled rows, suspect-false first, then by time. With `only_uncertain` also the auto-labelled rows the
    labeller was unsure of (unclear, confidence below kivi_autolabel.UNCERTAIN_CONF, true with unknown speaker); a row
    the owner labelled is never offered again."""
    if only_uncertain:
        import kivi_autolabel as A
        todo = [r for r in rows if A.is_uncertain(r)]
    else:
        todo = [r for r in rows if not r["label"]]
    if only_uncertain:
        return sorted(todo, key=lambda r: (A.review_priority(r), r["bucket"] != "suspect-false", r["utc"], r["device"]))
    return sorted(todo, key=lambda r: (r["bucket"] != "suspect-false", r["utc"], r["device"]))


def save_owner_label(table_path, run_id, label, speaker):
    """Write one owner decision into a FRESH read of the table (another tool may have written it meanwhile)."""
    fresh = read_table(table_path)
    for r in fresh:
        if r["run_id"] == run_id:
            r["label"], r["speaker"] = label, speaker
            r["label_source"], r["label_conf"], r["speaker_conf"] = "owner", "1.00", "1.00"
    write_table(table_path, fresh)


def ask(prompt, valid, input_fn):
    while True:
        s = input_fn(prompt).strip().lower()
        if s in valid:
            return s


def miss_table_path(a):
    """`--table` as given, else a bare name under `<dataset>/live-misses/`."""
    p = Path(a.table).expanduser()
    return p if (p.exists() or p.parent != Path(".")) else Path(a.dataset_root).expanduser() / "live-misses" / p.name


def read_miss_table(path):
    p = Path(path)
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return [{k: (row.get(k) or "") for k in MISS_FIELDS} for row in csv.DictReader(f)]


def write_miss_table(path, rows):
    write_csv_atomic(path, MISS_FIELDS, rows)


SPEAKER_KEYS = {"1": "spk01", "2": "spk02", "3": "spk-other", "4": "unknown"}
LIVE_KEYS = {"t": "true", "f": "false", "n": "not-a-wake", "u": "unclear"}


def save_miss_label(table_path, item_id, label, speaker):
    """Write one owner decision into a FRESH read of a window table (`save_owner_label` for the live table)."""
    fresh = read_miss_table(table_path)
    for r in fresh:
        if r["id"] == item_id:
            r["label"], r["speaker"] = label, speaker
    write_miss_table(table_path, fresh)


def review_window_item(r, base, play, input_fn):
    """Play one 6 s window and ask. Returns "q", "s" or (label, speaker)."""
    while True:
        play(base / r["clip"])
        k = ask("label [a/o/t/n/u], r = replay, s = skip, q = quit: ", tuple(MISS_LABELS) + ("r", "s", "q"), input_fn)
        if k != "r":
            break
    if k in ("q", "s"):
        return k
    label = MISS_LABELS[k]
    speaker = ""
    if label in ("attempt", "other-speech"):
        speaker = SPEAKER_KEYS[ask("speaker [1 = spk01, 2 = spk02, 3 = spk-other, 4 = unknown]: ", tuple(SPEAKER_KEYS), input_fn)]
    return label, speaker


def review_live_item(r, live, play, input_fn):
    """Play a live clip (wake tail, then the request) and ask. Returns "q", "s" or (label, speaker)."""
    d = live / r["device"] / r["utc"]
    while True:
        play(d / "wake_tail.wav")
        play(d / "stt.wav")
        k = ask("label [t/f/n/u], r = replay, s = skip, q = quit: ", ("t", "f", "n", "u", "r", "s", "q"), input_fn)
        if k != "r":
            break
    if k in ("q", "s"):
        return k
    sp = ask("speaker [1 = spk01, 2 = spk02, 3 = spk-other, 4 = unknown]: ", tuple(SPEAKER_KEYS), input_fn)
    return LIVE_KEYS[k], SPEAKER_KEYS[sp]


def review_validation_item(r, live, play, input_fn):
    """Blind validation clip: play it and ask. Returns "q", "s" or (label, cause, speaker). Nothing about the clip but
    its position is shown."""
    d = live / r["device"] / r["utc"]
    while True:
        play(d / "wake_tail.wav")
        play(d / "stt.wav")
        k = ask("label [t/f/n/u], r = replay, s = skip, q = quit: ", ("t", "f", "n", "u", "r", "s", "q"), input_fn)
        if k != "r":
            break
    if k in ("q", "s"):
        return k
    cause = ""
    if k == "f":
        cause = CAUSE_KEYS[ask("cause [o = other speech, m = tv / media, n = noise]: ", tuple(CAUSE_KEYS), input_fn)]
    speaker = ""
    if k in ("t", "u") or cause == "other-speech":
        speaker = SPEAKER_KEYS[ask("speaker [1 = spk01, 2 = spk02, 3 = spk-other, 4 = unknown]: ", tuple(SPEAKER_KEYS), input_fn)]
    return LIVE_KEYS[k], cause, speaker


def cmd_review_misses(a, player=None, input_fn=input, out=print):
    table_path = miss_table_path(a)
    rows = read_miss_table(table_path)
    if not rows:
        raise SystemExit(f"no table at {table_path}: run kivi_replay_mine.py candidates first")
    base = table_path.parent
    play = player or make_player(a.player)
    key = lambda r: (float(r["rank"] or 0) * -1, r["id"])      # noqa: E731
    todo = sorted(rows if a.again else [r for r in rows if not r["label"]], key=key)
    out(f"{len(todo)} window(s) to label, {sum(1 for r in rows if r['label'])} already labelled. Each is 6 s; the wake word, "
        "if any, is around the middle (3 s).")
    out("keys: a = attempt (someone said oye Kivi / Kivi to it), o = other speech, t = tv / media, n = noise, u = unclear, "
        "r = replay, s = skip, q = quit (progress is saved after every window)")
    done = 0
    try:
        for i, r in enumerate(todo, 1):
            out(f"\n[{i}/{len(todo)}] {r['id']} {r['device']} {r['utc']} ({r['part']})")
            res = review_window_item(r, base, play, input_fn)
            if res == "q":
                break
            if res == "s":
                continue
            r["label"], r["speaker"] = res
            save_miss_label(table_path, r["id"], *res)
            done += 1
    except (KeyboardInterrupt, EOFError):
        out("\ninterrupted")
    out(f"labelled {done} window(s) in this run; table saved ({table_path})")
    return done


def cmd_review(a, player=None, input_fn=input, out=print):
    if getattr(a, "table", None):
        return cmd_review_misses(a, player, input_fn, out)
    root = Path(a.dataset_root).expanduser()
    live = root / "live"
    table_path = live / "live_wakes.csv"
    rows = read_table(table_path)
    if not rows:
        raise SystemExit(f"no table at {table_path}: run extract first")
    play = player or make_player(a.player)
    todo = review_order(rows, getattr(a, "only_uncertain", False))
    if a.again:
        todo = sorted(rows, key=lambda r: (r["bucket"] != "suspect-false", r["utc"], r["device"]))
    out(f"{len(todo)} clip(s) to label, {len(rows) - len([r for r in rows if not r['label']])} already labelled. "
        "For each clip you hear the wake tail, then the request audio.")
    out("keys: t = true wake, f = false wake, n = not a wake (button press), u = unclear, r = replay, s = skip, "
        "q = quit (progress is saved after every clip)")
    done = 0
    try:
        for i, r in enumerate(todo, 1):
            out(f"\n[{i}/{len(todo)}] {r['device']} {r['model']} {r['utc']}  wake {r['wake_len']} s, stt {r['stt_len']} s, {r['bucket']}")
            res = review_live_item(r, live, play, input_fn)
            if res == "q":
                break
            if res == "s":
                continue
            r["label"], r["speaker"] = res
            save_owner_label(table_path, r["run_id"], *res)
            done += 1
    except (KeyboardInterrupt, EOFError):
        out("\ninterrupted")
    out(f"labelled {done} clip(s) in this run; table saved ({table_path})")
    return done


# ---------------------------------------------------------------------------------------------
# queue (owner only): every pending item of every table, in priority order
# ---------------------------------------------------------------------------------------------

FALSE_WAKE_TABLE = "daily_false_wakes.csv"
VALIDATION_TABLE = "autolabel_validation.csv"      # in live/; built by kivi_false_wake_audit.py sample, never by hand
VALIDATION_KEY = "autolabel_validation_key.csv"    # in live/; the frozen auto labels, never shown and never read here
VALIDATION_FIELDS = ("sample_id", "run_id", "device", "utc", "label", "cause", "speaker")
CAUSE_KEYS = {"o": "other-speech", "m": "tv-media", "n": "noise"}
QUEUE_GROUPS = ("autolabel validation sample (blind)", "replay false wakes of the live model", "uncertain live clips",
                "missed-wake candidates")
G_VALIDATION, G_FALSE_WAKES, G_UNCERTAIN, G_MISSES = range(4)


def window_tables(root):
    """Every window review table of `<dataset>/live-misses/` (header has id and clip), sorted by name."""
    out = []
    for p in sorted((Path(root) / "live-misses").glob("*.csv")):
        try:
            head = p.open(newline="", encoding="utf-8").readline().strip().split(",")
        except OSError:
            continue
        if "id" in head and "clip" in head and "label" in head:
            out.append(p)
    return out


def read_validation(path):
    p = Path(path)
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return [{k: (row.get(k) or "") for k in VALIDATION_FIELDS} for row in csv.DictReader(f)]


def save_validation_label(root, sample_id, label, cause, speaker):
    """Write one blind-sample decision (fresh read) and carry it into live_wakes.csv as an owner label, so the clip is
    not asked for again in the uncertain-clips group. The auto label stays only in the frozen key, which the analysis
    reads."""
    root = Path(root)
    table = root / "live" / VALIDATION_TABLE
    fresh = read_validation(table)
    run_id = ""
    for r in fresh:
        if r["sample_id"] == sample_id:
            r["label"], r["cause"], r["speaker"] = label, cause, speaker
            run_id = r["run_id"]
    write_csv_atomic(table, VALIDATION_FIELDS, fresh)
    live_path = root / "live" / "live_wakes.csv"
    if run_id and live_path.exists():
        save_owner_label(live_path, run_id, label, speaker or "unknown")


def queue_items(root, day=None, device=None):
    """Pending items, in the order the owner meets them: [{"group": 0..3, "kind": "validation"|"live"|"window",
    "table", "row"}]. Group 0 = the blind autolabel validation sample, 1 = the replay false-wake table, 2 = uncertain
    live clips, 3 = every other window table (best rank first). A clip that waits in the validation sample is not
    offered again in group 2 (the validation label is carried into live_wakes.csv as an owner label)."""
    root = Path(root)
    items = []
    keep = lambda r: (device is None or r["device"] == device) and (day is None or local_day(r["utc"]) == day)   # noqa: E731
    val_path = root / "live" / VALIDATION_TABLE
    val_rows = read_validation(val_path)
    pending_val = {r["run_id"] for r in val_rows if not r["label"]}
    items += [{"group": G_VALIDATION, "kind": "validation", "table": val_path, "row": r}
              for r in sorted(val_rows, key=lambda r: r["sample_id"]) if not r["label"] and keep(r)]
    for p in window_tables(root):
        group = G_FALSE_WAKES if p.name == FALSE_WAKE_TABLE else G_MISSES
        rows = [r for r in read_miss_table(p) if not r["label"] and keep(r)]
        rows.sort(key=(lambda r: (r["utc"], r["device"])) if group == G_FALSE_WAKES
                  else (lambda r: (-float(r["rank"] or 0), r["id"])))
        items += [{"group": group, "kind": "window", "table": p, "row": r} for r in rows]
    live_table = root / "live" / "live_wakes.csv"
    items += [{"group": G_UNCERTAIN, "kind": "live", "table": live_table, "row": r}
              for r in review_order(read_table(live_table), only_uncertain=True)
              if keep(r) and r["run_id"] not in pending_val]
    items.sort(key=lambda it: it["group"])           # stable: the order inside a group is kept
    return items


def queue_counts(items):
    """{(group, table name): pending}, in queue order."""
    cnt = Counter((it["group"], it["table"].name) for it in items)
    return dict(sorted(cnt.items()))


def print_queue_counts(items, out, head):
    cnt = queue_counts(items)
    out(f"{head}: {len(items)} item(s)" + ("" if cnt else " (queue empty)"))
    for (g, name), n in cnt.items():
        out(f"  {g + 1}. {QUEUE_GROUPS[g]}: {name}: {n}")


def cmd_queue(a, player=None, input_fn=input, out=print, clock=None):
    import time as _time
    clock = clock or _time.monotonic
    root = Path(a.dataset_root).expanduser()
    items = queue_items(root, getattr(a, "day", None), getattr(a, "device", None))
    print_queue_counts(items, out, "pending at start")
    if not items:
        return 0
    play = player or make_player(a.player)
    live = root / "live"
    out("keys per item (shown with it); r = replay, s = skip (comes back next time), q = quit; "
        "every label is saved at once, so you can stop whenever you like")
    t0, done, limit = clock(), 0, getattr(a, "max_items", None)
    end_s = getattr(a, "minutes", None) * 60 if getattr(a, "minutes", None) else None
    try:
        for i, it in enumerate(items, 1):
            if (limit is not None and done >= limit) or (end_s is not None and clock() - t0 >= end_s):
                out("session limit reached")
                break
            r = it["row"]
            if it["kind"] == "validation":
                out(f"\n[{i}/{len(items)}] validation clip {r['sample_id']} (blind; label what you hear: t = true "
                    "oye Kivi attempt, f = false wake (then why), n = not a wake / button, u = unclear)")
                res = review_validation_item(r, live, play, input_fn)
            elif it["kind"] == "live":
                out(f"\n[{i}/{len(items)}] live clip: {r['device']} {r['utc']}  wake {r['wake_len']} s, stt {r['stt_len']} s "
                    "(t = true wake, f = false wake, n = not a wake / button, u = unclear)")
                res = review_live_item(r, live, play, input_fn)
            else:
                out(f"\n[{i}/{len(items)}] {QUEUE_GROUPS[it['group']]}: {r['id']} {r['device']} {r['utc']} "
                    "(6 s window; a = attempt, o = other speech, t = tv / media, n = noise, u = unclear)")
                res = review_window_item(r, it["table"].parent, play, input_fn)
            if res == "q":
                break
            if res == "s":
                continue
            if it["kind"] == "validation":
                save_validation_label(root, r["sample_id"], *res)
            elif it["kind"] == "live":
                save_owner_label(it["table"], r["run_id"], *res)
            else:
                save_miss_label(it["table"], r["id"], *res)
            done += 1
    except (KeyboardInterrupt, EOFError):
        out("\ninterrupted")
    out(f"labelled {done} item(s) in this session")
    print_queue_counts(queue_items(root, getattr(a, "day", None), getattr(a, "device", None)), out, "pending at end")
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


def propose_split_alternate(rows):
    """Alternate-day rule (owner decision 2026-11-11): the Europe/Madrid day of the month is odd -> test, even -> train.
    Only rows labelled true or false are assigned (unclear and not-a-wake rows are not training or test material)."""
    return {r["run_id"]: ("test" if int(local_day(r["utc"])[-2:]) % 2 else "train")
            for r in rows if r["label"] in ("true", "false")}


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
    if getattr(a, "rule", "alternate-day") == "alternate-day":
        assign = propose_split_alternate(rows)
        cnt = Counter((r["label"], r["speaker"] or "unknown", r["device"], assign[r["run_id"]])
                      for r in rows if r["run_id"] in assign)
        out(f"rule: alternate-day ({TZ} day of month odd = test, even = train); rows: label / speaker / device / split")
        for k, n in sorted(cnt.items()):
            out("  " + " / ".join(k) + f": {n}")
        info = {}
    else:
        assign, info = propose_split(rows, a.test_share)
    for sp, i in info.items():
        out(f"  {sp}: {i['true']} true wake(s) on {i['days']} local day(s) ({TZ}); "
            + (f"test = {len(i['test_days'])} most recent day(s), {i['test']} true wake(s) "
               f"({i['test'] / i['true']:.0%}); train {i['train']}" if i["test_days"] else i["note"]))
    fcnt = Counter(assign[r["run_id"]] for r in rows if r["label"] == "false" and r["run_id"] in assign)
    out("  split totals: " + (", ".join(f"{k} {n}" for k, n in sorted(Counter(assign.values()).items())) or "none"))
    out("  rows labelled false: " + (", ".join(f"{s} {n}" for s, n in sorted(fcnt.items())) or "none"))
    out(f"proposal covers {len(assign)} of {len(rows)} row(s)")
    if a.apply:
        import kivi_autolabel as A
        if not A.wait_for_review(getattr(a, "pgrep", None), out=out, **getattr(a, "wait_kw", {})):
            raise SystemExit("a review process is still running: split not applied (owner labels would be at risk)")
        fresh = read_table(table_path)                       # fresh read: another tool may have written meanwhile
        for r in fresh:
            if r["run_id"] in assign:
                r["split"] = assign[r["run_id"]]
        write_table(table_path, fresh)
        out("applied to the split column")
    else:
        out("proposal only; --apply writes it into the table")
    return assign


# ---------------------------------------------------------------------------------------------
# autolabel (local; no transcripts leave memory, see kivi_autolabel.py)
# ---------------------------------------------------------------------------------------------


def cmd_trigger_only(a, out=print, scorer=None):
    """Recompute only the model-derived cells (trigger, peak_t_s, peak_raw, the model parts of auto_evidence) with the
    exact replay of the live model; no transcription, no speaker embedding (faster-whisper and ECAPA are not loaded).
    Labels and speakers are never changed (see kivi_autolabel.merge_trigger_only)."""
    import kivi_autolabel as A
    root = Path(a.dataset_root).expanduser()
    live = root / "live"
    table_path = live / "live_wakes.csv"
    rows = read_table(table_path)
    if not rows:
        raise SystemExit(f"no table at {table_path}: run extract first")
    if scorer is None:
        scorer = A.ModelScorer(getattr(a, "work", None) or str(C.kivi_home()))
    results, missing = {}, 0
    for r in rows:
        res = A.trigger_only(r, live / r["device"] / r["utc"], scorer)
        if res is None:
            missing += 1
        else:
            results[r["run_id"]] = res
    before = Counter(r["trigger"] or "empty" for r in rows)
    xt = Counter((res["cross"][0], res["cross"][1]) for res in results.values())
    out(f"trigger-only: {len(results)} row(s) replayed, {missing} without a tail; trigger before: "
        + ", ".join(f"{k} {n}" for k, n in sorted(before.items())))
    out("crossing (a x c): " + ", ".join(f"{k[0]}x{k[1]} {n}" for k, n in sorted(xt.items())))
    if getattr(a, "dry_run", False):
        fresh = read_table(table_path)
        n = A.merge_trigger_only(fresh, results)
        out("trigger after: " + ", ".join(f"{k} {c}" for k, c in sorted(Counter(r["trigger"] or "empty" for r in fresh).items()))
            + f"; {dict(n)}; dry run: table not written")
        return results
    if not A.wait_for_review(getattr(a, "pgrep", None), out=out, **getattr(a, "wait_kw", {})):
        raise SystemExit("a review process is still running after 15 min: table not written")
    backup = table_path.with_name(table_path.name + ".bak-" + A.utc_stamp())
    shutil.copy2(table_path, backup)
    os.chmod(backup, 0o600)
    fresh = read_table(table_path)                           # fresh read right before the write: owner labels win
    owner_before = [(r["run_id"], r["label"], r["label_source"], r["speaker"]) for r in fresh if r["label"] and not r["label_source"].startswith("auto")]
    n = A.merge_trigger_only(fresh, results)
    owner_after = [(r["run_id"], r["label"], r["label_source"], r["speaker"]) for r in fresh if r["label"] and not r["label_source"].startswith("auto")]
    if owner_before != owner_after:
        raise SystemExit("owner cells changed: table not written (bug)")
    write_table(table_path, fresh)
    out("trigger after: " + ", ".join(f"{k} {c}" for k, c in sorted(Counter(r["trigger"] or "empty" for r in fresh).items()))
        + f"; written: {dict(n)}; backup {backup.name}")
    return results


ERA_EDGE_S = 10.0          # a detection this close to an era edge is reported (the run start is boot + ns, +-5 s)


def detection_epoch(row):
    """Detection time of a row: run start (`utc`) plus the wake file length."""
    t = datetime.strptime(row["utc"], "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
    return t + float(row.get("wake_len") or 0)


def era_fixes(rows):
    """{run_id: (old, new)} for the rows whose `model` is not the model of their era (`model_of` at the detection time),
    and the run ids within ERA_EDGE_S of an era edge (pure)."""
    edges = [epoch(x) for _m, lo, hi, _d in ERAS for x in (lo, hi) if x]
    fix, near = {}, []
    for r in rows:
        t = detection_epoch(r)
        m = model_of(r["device"], t)
        if m and m != r["model"]:
            fix[r["run_id"]] = (r["model"], m)
        if any(abs(t - e) < ERA_EDGE_S for e in edges):
            near.append(r["run_id"])
    return fix, near


def cmd_fix_models(a, out=print):
    """Set the `model` column to the model of each row's era (ERAS); only that column changes. Afterwards run
    `autolabel --trigger-only` so the trigger and peak cells use the right model."""
    import kivi_autolabel as A
    table_path = Path(a.dataset_root).expanduser() / "live" / "live_wakes.csv"
    rows = read_table(table_path)
    fix, near = era_fixes(rows)
    out(f"fix-models: {len(fix)} row(s) to change: "
        + ", ".join(f"{o or 'empty'} -> {n} {c}" for (o, n), c in sorted(Counter(fix.values()).items()))
        + (f"; {len(near)} row(s) within {ERA_EDGE_S:.0f} s of an era edge: {', '.join(near)}" if near else ""))
    if getattr(a, "dry_run", False) or not fix:
        return fix
    if not A.wait_for_review(getattr(a, "pgrep", None), out=out, **getattr(a, "wait_kw", {})):
        raise SystemExit("a review process is still running after 15 min: table not written")
    backup = table_path.with_name(table_path.name + ".bak-" + A.utc_stamp() + "-pre-fix-models")
    shutil.copy2(table_path, backup)
    os.chmod(backup, 0o600)
    fresh = read_table(table_path)                       # fresh read right before the write
    keep = [{k: v for k, v in r.items() if k != "model"} for r in fresh]
    fix, _ = era_fixes(fresh)
    for r in fresh:
        if r["run_id"] in fix:
            r["model"] = fix[r["run_id"]][1]
    if keep != [{k: v for k, v in r.items() if k != "model"} for r in fresh]:
        raise SystemExit("cells other than model changed: table not written (bug)")
    write_table(table_path, fresh)
    out(f"written: {len(fix)} model cell(s); backup {backup.name}; next: autolabel --trigger-only")
    return fix


def cmd_autolabel(a, out=print, transcriber=None, embedder=None, scorer=None):
    if getattr(a, "trigger_only", False):
        return cmd_trigger_only(a, out=out, scorer=scorer)
    import kivi_autolabel as A
    root = Path(a.dataset_root).expanduser()
    live = root / "live"
    table_path = live / "live_wakes.csv"
    rows = read_table(table_path)
    if not rows:
        raise SystemExit(f"no table at {table_path}: run extract first")
    work = getattr(a, "work", None) or str(C.kivi_home())
    own_models = []
    if transcriber is None:
        transcriber = A.FasterWhisperTranscriber()
    if embedder is None:
        embedder = A.EcapaEmbedder()
        own_models.append(embedder)
    if scorer is None:
        scorer = A.ModelScorer(work)
    try:
        clips = A.enrolment_clips(root) if not getattr(a, "enrol", None) else a.enrol
        items, groups = A.enrol(clips, embedder)
        thr, rep = A.calibrate(items, groups)
        centr = A.centroids(items)
        out(f"enrolment: {Counter(s for s, _ in items)}; leave-one-take-out {rep['correct']}/{rep['n']} correct, "
            f"{rep['unknown']} unknown, {rep['wrong']} wrong; per speaker {rep['per_speaker']}")
        if "leave_group_out" in rep:
            out(f"leave-one-session-out: {rep['leave_group_out']}")
        out("thresholds: " + ", ".join(f"{k} {v:.3f}" for k, v in thr.items()))
        results = {}
        for r in rows:
            if getattr(a, "day", None) and (local_day(r["utc"]) != a.day or r["label"]):
                continue                         # daily loop: only that day's rows that have no label yet
            results[r["run_id"]] = A.analyse_clip(r, live / r["device"] / r["utc"], transcriber, embedder, scorer, centr,
                                                  thr)
    finally:
        for m in own_models:
            m.close()
    ag = A.agreement(rows, results)
    xt = Counter((res["cross"][0], res["cross"][1]) for res in results.values())
    out("trigger check (a = request audio in the wake run `same` / separate stt-only run `sep`; c = live model score "
        "in the last 2 s `cross` / `weak` / `none`): " + ", ".join(f"{k[0]}x{k[1]} {n}" for k, n in sorted(xt.items())))
    out("auto labels: " + ", ".join(f"{k} {n}" for k, n in sorted(Counter(r["label"] for r in results.values()).items())))
    out("agreement with owner cells present at the start: " + (", ".join(f"{k} {n}" for k, n in sorted(ag.items())) or
                                                               "no owner cells"))
    if getattr(a, "dry_run", False):
        out("dry run: table not written")
        return results
    if not A.wait_for_review(getattr(a, "pgrep", None), out=out, **getattr(a, "wait_kw", {})):
        raise SystemExit("a review process is still running after 15 min: table not written")
    backup = table_path.with_name(table_path.name + ".bak-" + A.utc_stamp())
    shutil.copy2(table_path, backup)
    os.chmod(backup, 0o600)
    fresh = read_table(table_path)                           # fresh read right before the write: owner labels win
    owner_before = sum(1 for r in fresh if r["label"] and not r["label_source"].startswith("auto"))
    n = A.merge_results(fresh, results, redo=getattr(a, "redo", False))
    write_table(table_path, fresh)
    out(f"owner labels present at write time: {owner_before}; written: {dict(n)}; backup {backup.name}")
    return results


# ---------------------------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["list", "extract", "review", "queue", "split", "autolabel", "import-button-labels",
                                     "fix-models"])
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
    ap.add_argument("--player", help="(review, queue) player command, the file is appended; default pw-play, else aplay -q")
    ap.add_argument("--again", action="store_true", help="(review) go through labelled clips too")
    ap.add_argument("--table", help="(review) label the missed-wake candidates of this table (miss_candidates.csv) instead of the live wakes")
    ap.add_argument("--apply", action="store_true", help="(split) write the proposal into the split column")
    ap.add_argument("--test-share", type=float, default=TEST_SHARE, help="(split) share of true wakes in test, default 0.30")
    ap.add_argument("--only-uncertain", action="store_true",
                    help="(review) only unlabelled rows and auto labels that are unclear, low confidence or true with unknown speaker")
    ap.add_argument("--rule", choices=["alternate-day", "recent-days"], default="alternate-day",
                    help="(split) alternate-day: odd Madrid day of month = test, even = train (default); "
                         "recent-days: the older share-per-speaker proposal")
    ap.add_argument("--dry-run", action="store_true", help="(autolabel, fix-models) analyse and print counts, write nothing")
    ap.add_argument("--redo", action="store_true", help="(autolabel) recompute cells that an earlier autolabel run wrote")
    ap.add_argument("--trigger-only", action="store_true",
                    help="(autolabel) recompute only trigger, peak_t_s, peak_raw and the model parts of auto_evidence "
                         "(exact replay); no transcription or speaker embedding; labels and speakers untouched")
    ap.add_argument("--day", help="(extract, autolabel, queue) one Europe/Madrid day YYYY-MM-DD, by run start; "
                                  "autolabel then analyses only that day's rows that have no label yet")
    ap.add_argument("--device", help="(queue) only this device")
    ap.add_argument("--minutes", type=float, help="(queue) stop after this many minutes")
    ap.add_argument("--max-items", type=int, help="(queue) stop after this many labels")
    ap.add_argument("--since", help="(import-button-labels) ISO time, default the helper's deployment 2026-11-11T17:30:00Z")
    ap.add_argument("--until", help="(import-button-labels) ISO time, default now")
    ap.add_argument("--window-s", type=float, help="(import-button-labels) a press labels a run whose detection ended this "
                                                   "many seconds or fewer before it (default 60)")
    ap.add_argument("--presses-file", help='(import-button-labels) JSON {"c3-a": [ISO times]} instead of the recorder')
    ap.add_argument("--pg-pod", default="home-assistant-postgres-0")
    ap.add_argument("--pg-container", default="postgres")
    ap.add_argument("--pg-user", default="postgres")
    ap.add_argument("--pg-db", default="home_assistant")
    ap.add_argument("--work", help="(autolabel, extract) workspace holding the model files, default $WORK")
    a = ap.parse_args(argv)
    {"list": cmd_list, "extract": cmd_extract, "review": cmd_review, "queue": cmd_queue, "split": cmd_split,
     "autolabel": cmd_autolabel, "import-button-labels": cmd_import_button_labels, "fix-models": cmd_fix_models}[a.what](a)


if __name__ == "__main__":
    main()
