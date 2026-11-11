#!/usr/bin/env python3
"""Copy closed wake-stage files of the satellites into the dataset as real TV-room negatives.

    kivi_negatives.py --dry-run                         # what it would copy (counts and hours only)
    kivi_negatives.py --hours 30                        # about 30 h split over the devices in kivi_devices.json
    kivi_negatives.py --hours 1000 --since 2026-11-08T13:30:00Z --until 2026-11-11T16:30:00Z \
        --exclude-pipeline "Kiwi Test" --out-name negatives-v5era --part test2-v5era
                                                        # a fresh set of every closed file of a period, in its own directory

NOT run by the tools that build the dataset: the owner (or the orchestrator, on the owner's word)
starts it. It reads the pod with `kubectl exec ... stat/dd` only (byte ranges of the pieces it keeps). This is household audio, the
satellites' continuous room sound: see the data register. It lands under
`<dataset>/negatives/<device>/<run>-<k>.wav` with `<dataset>/negatives/manifest.jsonl`.

What is kept: closed files only (not written for `--min-age-s`), minus
- the first 0.3 s of every file (stream start-up) and the last 7 s of every closed file (the
  detection that closed it; the wake word itself may be there),
- 7 s after the start of a run that follows within `--followup-s` of another run (the sound after a
  detection: the reply and the follow-up),
- every window within 10 s of a button press (`input_button.kivi_take`) or of a cue of a recorded
  session or button-take in the dataset (so spoken "kivi" never ends up as a negative).
- the whole window of every recorded session of that device (start - 120 s to end + 120 s), so the
  positives, near-words and the talking around them never become negatives,
- everything from `--exclude-after DEVICE=ISO_TIME` onward (repeatable; e.g. while a new session is
  being recorded on that device) and everything of a device between two times
  (`--exclude-between DEVICE=START,END`, repeatable; closes such a window once the session is over),
- with `--since/--until` only files that started at or after SINCE and were last written at or before
  UNTIL (a run that began before SINCE ran with the earlier wake word and is not part of the period),
  and with `--exclude-pipeline NAME` (repeatable) no file of that pipeline directory at all.
`--out-name` puts the pieces in `<dataset>/<out-name>/` with its own manifest (default `negatives`, so
the set the training tools read is untouched) and `--part` is the `split` value written in the rows
(default `unassigned`; `test2-v5era` for the held-out set of the v5 era).
Pieces shorter than `--min-seg-s` are dropped. Files are picked at random (seeded) per device until the
hours are reached. Nothing is deleted or overwritten; re-running skips what is already there.
"""
import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kivi_extract as X  # noqa: E402
import kivi_real_common as C  # noqa: E402

RATE = C.RATE


def known_marks(root, presses):
    """Epoch times of everything that must stay out of the negatives: presses plus the cues and
    presses already recorded in the dataset."""
    marks = list(presses)
    for sj in Path(root).glob("*/session.json"):
        try:
            s = json.loads(sj.read_text(encoding="utf-8"))
        except Exception:
            continue
        if s.get("t0_utc") and s.get("blocks"):
            t0 = X.parse_utc(s["t0_utc"]).timestamp()
            marks += [t0 + c["offset_s"] for b in s["blocks"] for c in b.get("cues", [])]
    for r in C.read_jsonl(Path(root) / "manifest.jsonl"):
        if r.get("press_utc"):
            marks.append(X.parse_utc(r["press_utc"]).timestamp())
    return sorted(marks)


def subtract(intervals, cuts):
    """`intervals` minus `cuts`, both lists of (a, b)."""
    out = []
    for a, b in intervals:
        cur = a
        for c0, c1 in sorted(cuts):
            if c1 <= cur or c0 >= b:
                continue
            if c0 > cur:
                out.append((cur, c0))
            cur = max(cur, c1)
        if cur < b:
            out.append((cur, b))
    return out


def session_windows(root, margin_s=120.0):
    """{device: [(t0, t1)]}: the recorded sessions of the dataset with a margin on both sides."""
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


def parse_exclude_between(items):
    """['c3-a=2026-11-07T11:50:00Z,2026-11-07T12:24:40Z'] -> {'c3-a': [(start_epoch, end_epoch)]}."""
    out = {}
    for it in items or []:
        dev, _, rng = it.partition("=")
        lo, _, hi = rng.partition(",")
        if not (dev and lo and hi):
            raise SystemExit(f"--exclude-between needs DEVICE=START,END, got {it!r}")
        t0, t1 = X.parse_when(lo), X.parse_when(hi)
        if t1 <= t0:
            raise SystemExit(f"--exclude-between: END must be after START in {it!r}")
        out.setdefault(dev.strip(), []).append((t0, t1))
    return out


def parse_exclude_after(items):
    """['c3-a=2026-11-07T11:50:00Z'] -> {'c3-a': epoch}."""
    out = {}
    for it in items or []:
        dev, _, when = it.partition("=")
        if not dev or not when:
            raise SystemExit(f"--exclude-after needs DEVICE=ISO_TIME, got {it!r}")
        out[dev.strip()] = X.parse_when(when)
    return out


def plan_file(f, prev_end, marks, a, windows=(), after=None):
    """Kept pieces of one closed file, in epoch seconds: [(t0, t1)]. `windows` are intervals to leave out
    entirely, `after` an epoch time from which nothing is kept."""
    lo = f["start"] + a.trim_start_s
    if prev_end is not None and 0 <= f["start"] - prev_end <= a.followup_s:
        lo = max(lo, f["start"] + a.boundary_s)
    hi = f["mtime"] - a.boundary_s
    if hi - lo < a.min_seg_s:
        return []
    if after is not None:
        hi = min(hi, after)
        if hi - lo < a.min_seg_s:
            return []
    cuts = [(m - a.exclude_s, m + a.exclude_s) for m in marks if f["start"] - a.exclude_s <= m <= f["mtime"] + a.exclude_s]
    cuts += [(w0, w1) for w0, w1 in windows if w1 > f["start"] and w0 < f["mtime"]]
    return [(x, y) for x, y in subtract([(lo, hi)], cuts) if y - x >= a.min_seg_s]


def period_filter(files, since=None, until=None, exclude_pipelines=()):
    """Files that started at or after `since`, were last written at or before `until` and are not in an
    excluded pipeline directory (exact name). The follow-up trim of a file still uses its real predecessor."""
    return [f for f in files
            if (since is None or f["start"] >= since) and (until is None or f["mtime"] <= until)
            and f["pipeline"] not in set(exclude_pipelines or ())]


def run(a, fetcher=None, presses=None):
    root = Path(a.dataset_root).expanduser()
    devs = [d.strip() for d in a.devices.split(",")] if a.devices else list(C.load_devices(a.devices_file))
    fetcher = fetcher or X.KubectlFetcher(a.context, a.namespace, a.pod, a.container, a.recordings_root)
    now = time.time() if a.now is None else a.now
    plans = {}
    all_files = {}
    for d in devs:
        prefix = C.device_prefix(d, a.devices_file)
        files = []
        all_files[d] = X.list_files(fetcher, prefix, "wake")
    spans = [(f["start"], f["mtime"]) for fs in all_files.values() for f in fs]
    if presses is None and not a.no_presses:
        presses = (X.FilePresses(a.presses_file) if a.presses_file else
                   X.PostgresPresses(a.context, a.namespace, a.pg_pod, a.pg_container, a.pg_user, a.pg_db))
    presses_t = presses.presses(min(s for s, _ in spans) - 60, max(e for _, e in spans) + 60) if presses and spans else []
    marks = known_marks(root, presses_t)
    swin = session_windows(root)
    after = parse_exclude_after(getattr(a, "exclude_after", None))
    between = parse_exclude_between(getattr(a, "exclude_between", None))
    for d, w in between.items():
        swin[d] = list(swin.get(d, [])) + w
    rng = random.Random(a.seed)
    target = a.hours * 3600 / max(1, len(devs))
    out_name = getattr(a, "out_name", None) or "negatives"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", out_name):
        raise SystemExit(f"bad --out-name {out_name!r}")
    part = getattr(a, "part", None) or "unassigned"
    since = X.parse_when(a.since) if getattr(a, "since", None) else None
    until = X.parse_when(a.until) if getattr(a, "until", None) else None
    excl_pipes = list(getattr(a, "exclude_pipeline", None) or [])
    manifest = root / out_name / "manifest.jsonl"
    done = {(r["device"], r["source_run"], r["start_utc"]) for r in C.read_jsonl(manifest)}
    summary = {}
    for d in devs:
        files = all_files[d]
        prev_end = {id(f): (files[i - 1]["mtime"] if i else None) for i, f in enumerate(files)}
        closed = period_filter([f for f in files if now - f["mtime"] >= a.min_age_s], since, until, excl_pipes)
        order = closed[:]
        rng.shuffle(order)
        picked, total = [], 0.0
        for f in order:
            if total >= target:
                break
            segs = plan_file(f, prev_end[id(f)], marks, a, swin.get(d, ()), after.get(d))
            if segs:
                picked.append((f, segs))
                total += sum(y - x for x, y in segs)
        plans[d] = picked
        summary[d] = {"files_listed": len(files), "closed": len(closed), "files_picked": len(picked),
                      "pieces": sum(len(s) for _, s in picked), "hours": round(total / 3600, 2)}
    print(f"press and cue marks excluded: {len(marks)}; session windows excluded: "
          f"{ {d: len(w) for d, w in swin.items()} }; excluded from a time on: "
          f"{ {d: X.utc_str(t) for d, t in after.items()} }")
    for d, s in summary.items():
        print(f"  {d}: {s['files_listed']} files, {s['closed']} closed, picked {s['files_picked']} ({s['pieces']} pieces, {s['hours']} h)")
    if a.dry_run:
        return summary
    C.private_mkdir(root / out_name, root)
    rows = []
    for d, picked in plans.items():
        ddir = C.private_mkdir(root / out_name / d, root)
        src = C.source_label(d)
        for f, segs in picked:
            run_id = f"{f['pipeline']}/{f['run']}"
            todo = []
            for k, (t0, t1) in enumerate(segs, 1):
                key = (d, run_id, X.utc_str(t0))
                rel = f"{out_name}/{d}/{f['run']}-{k:02d}.wav"
                if key in done or (root / rel).exists():
                    continue
                s0 = max(0, int(round((t0 - f["start"]) * RATE)))
                s1 = min(f["n"], int(round((t1 - f["start"]) * RATE)))
                todo.append((s0, s1, C.PartWavWriter(root / rel), rel, t0, t1))
            if not todo:
                continue
            lo = 44 + 2 * min(t[0] for t in todo)
            hi = 44 + 2 * max(t[1] for t in todo)
            pos = lo                         # byte position in the file
            try:
                for chunk in fetcher.fetch_range(f["path"], lo, hi):
                    for s0, s1, w, *_ in todo:
                        b0, b1 = 44 + 2 * s0, 44 + 2 * s1
                        a0, a1 = max(b0, pos), min(b1, pos + len(chunk))
                        if a1 > a0:
                            w.write(chunk[a0 - pos:a1 - pos])
                    pos += len(chunk)
                for s0, s1, w, *_ in todo:       # the byte count must match the range, else the piece is not kept
                    w.commit(expected_bytes=2 * (s1 - s0))
            except Exception as e:
                for _, _, w, *_ in todo:
                    w.close()
                raise SystemExit(f"copy of {run_id} failed ({e}); unfinished .part files stay in {ddir}")
            for s0, s1, w, rel, t0, t1 in todo:
                row = C.make_row(
                    origin="negatives", session="negatives", source=src, device=d, session_device=d, speaker="",
                    split=part, eval_channel=True, condition="household", label="negative", kind="negative",
                    clip=rel, start_utc=X.utc_str(t0), end_utc=X.utc_str(t1), source_run=run_id, revision="")
                C.append_jsonl(manifest, [row])
                rows.append(row)
    print(f"wrote {len(rows)} pieces; manifest {manifest}")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dataset-root", default=C.DEFAULT_DATASET_ROOT)
    ap.add_argument("--hours", type=float, default=30.0, help="total, split evenly over the devices")
    ap.add_argument("--devices", help="comma list (default: every device in kivi_devices.json)")
    ap.add_argument("--devices-file")
    ap.add_argument("--context", default="grigri")
    ap.add_argument("--namespace", default="home-assistant")
    ap.add_argument("--pod", default="home-assistant-hass-0")
    ap.add_argument("--container")
    ap.add_argument("--recordings-root", default="/voice-recordings")
    ap.add_argument("--from-dir", help="local copy laid out like /voice-recordings")
    ap.add_argument("--presses-file", help="JSON list of ISO press times instead of the database")
    ap.add_argument("--no-presses", action="store_true", help="skip the button press lookup")
    ap.add_argument("--pg-pod", default="home-assistant-postgres-0")
    ap.add_argument("--pg-container", default="postgres")
    ap.add_argument("--pg-user", default="postgres")
    ap.add_argument("--pg-db", default="home_assistant")
    ap.add_argument("--exclude-after", action="append", metavar="DEVICE=ISO_TIME",
                    help="keep nothing of DEVICE from this time on (repeatable), e.g. c3-a=2026-11-07T11:50:00Z")
    ap.add_argument("--exclude-between", action="append", metavar="DEVICE=START,END",
                    help="keep nothing of DEVICE between two ISO times (repeatable), e.g. c3-a=2026-11-07T11:50:00Z,2026-11-07T12:24:40Z")
    ap.add_argument("--since", help="only files that started at or after this ISO time (UTC with Z, else local time)")
    ap.add_argument("--until", help="only files last written at or before this ISO time")
    ap.add_argument("--exclude-pipeline", action="append", metavar="NAME",
                    help="skip every file of this pipeline directory (exact name, repeatable), e.g. 'Kiwi Test'")
    ap.add_argument("--out-name", default="negatives", help="directory under the dataset root for the pieces and their manifest")
    ap.add_argument("--part", default="unassigned", help="the `split` value written in the manifest rows, e.g. test2-v5era")
    ap.add_argument("--min-age-s", type=float, default=600.0, help="a file is closed when not written for this long")
    ap.add_argument("--trim-start-s", type=float, default=0.3)
    ap.add_argument("--boundary-s", type=float, default=7.0)
    ap.add_argument("--followup-s", type=float, default=60.0)
    ap.add_argument("--exclude-s", type=float, default=10.0, help="around presses and session cues")
    ap.add_argument("--min-seg-s", type=float, default=10.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--now", type=float, help=argparse.SUPPRESS)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    run(a, X.LocalFetcher(a.from_dir) if a.from_dir else None)


if __name__ == "__main__":
    main()
