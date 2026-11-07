#!/usr/bin/env python3
"""Copy closed wake-stage files of the satellites into the dataset as real TV-room negatives.

    kivi_negatives.py --dry-run                         # what it would copy (counts and hours only)
    kivi_negatives.py --hours 30                        # about 30 h split over the devices in kivi_devices.json

NOT run by the tools that build the dataset: the owner (or the orchestrator, on the owner's word)
starts it. It reads the pod with `kubectl exec ... stat/cat` only. This is household audio, the
satellites' continuous room sound: see the data register. It lands under
`<dataset>/negatives/<device>/<run>-<k>.wav` with `<dataset>/negatives/manifest.jsonl`.

What is kept: closed files only (not written for `--min-age-s`), minus
- the first 0.3 s of every file (stream start-up) and the last 7 s of every closed file (the
  detection that closed it; the wake word itself may be there),
- 7 s after the start of a run that follows within `--followup-s` of another run (the sound after a
  detection: the reply and the follow-up),
- every window within 10 s of a button press (`input_button.kivi_take`) or of a cue of a recorded
  session or button-take in the dataset (so spoken "kivi" never ends up as a negative).
Pieces shorter than `--min-seg-s` are dropped. Files are picked at random (seeded) per device until the
hours are reached. Nothing is deleted or overwritten; re-running skips what is already there.
"""
import argparse
import json
import os
import random
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


def plan_file(f, prev_end, marks, a):
    """Kept pieces of one closed file, in epoch seconds: [(t0, t1)]."""
    lo = f["start"] + a.trim_start_s
    if prev_end is not None and 0 <= f["start"] - prev_end <= a.followup_s:
        lo = max(lo, f["start"] + a.boundary_s)
    hi = f["mtime"] - a.boundary_s
    if hi - lo < a.min_seg_s:
        return []
    cuts = [(m - a.exclude_s, m + a.exclude_s) for m in marks if f["start"] - a.exclude_s <= m <= f["mtime"] + a.exclude_s]
    return [(x, y) for x, y in subtract([(lo, hi)], cuts) if y - x >= a.min_seg_s]


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
        for f in fetcher.list_wake_files(prefix):
            dur = max(0, f["size"] - 44) / 2 / RATE
            f.update(dur=dur, start=f["mtime"] - dur, **X.run_of(f["path"]))
            files.append(f)
        files.sort(key=lambda f: f["start"])
        all_files[d] = files
    spans = [(f["start"], f["mtime"]) for fs in all_files.values() for f in fs]
    if presses is None and not a.no_presses:
        presses = (X.FilePresses(a.presses_file) if a.presses_file else
                   X.PostgresPresses(a.context, a.namespace, a.pg_pod, a.pg_container, a.pg_user, a.pg_db))
    presses_t = presses.presses(min(s for s, _ in spans) - 60, max(e for _, e in spans) + 60) if presses and spans else []
    marks = known_marks(root, presses_t)
    rng = random.Random(a.seed)
    target = a.hours * 3600 / max(1, len(devs))
    manifest = root / "negatives" / "manifest.jsonl"
    done = {(r["device"], r["source_run"], r["start_utc"]) for r in C.read_jsonl(manifest)}
    summary = {}
    for d in devs:
        files = all_files[d]
        prev_end = {id(f): (files[i - 1]["mtime"] if i else None) for i, f in enumerate(files)}
        closed = [f for f in files if now - f["mtime"] >= a.min_age_s]
        order = closed[:]
        rng.shuffle(order)
        picked, total = [], 0.0
        for f in order:
            if total >= target:
                break
            segs = plan_file(f, prev_end[id(f)], marks, a)
            if segs:
                picked.append((f, segs))
                total += sum(y - x for x, y in segs)
        plans[d] = picked
        summary[d] = {"files_listed": len(files), "closed": len(closed), "files_picked": len(picked),
                      "pieces": sum(len(s) for _, s in picked), "hours": round(total / 3600, 2)}
    print(f"press and cue marks excluded: {len(marks)}")
    for d, s in summary.items():
        print(f"  {d}: {s['files_listed']} files, {s['closed']} closed, picked {s['files_picked']} ({s['pieces']} pieces, {s['hours']} h)")
    if a.dry_run:
        return summary
    C.private_mkdir(root / "negatives", root)
    rows = []
    for d, picked in plans.items():
        ddir = C.private_mkdir(root / "negatives" / d, root)
        src = C.source_label(d)
        for f, segs in picked:
            run_id = f"{f['pipeline']}/{f['run']}"
            todo = []
            for k, (t0, t1) in enumerate(segs, 1):
                key = (d, run_id, X.utc_str(t0))
                rel = f"negatives/{d}/{f['run']}-{k:02d}.wav"
                if key in done or (root / rel).exists():
                    continue
                s0 = int(round((t0 - f["start"]) * RATE)) * 2
                s1 = int(round((t1 - f["start"]) * RATE)) * 2
                todo.append((s0, s1, C.WavWriter(root / rel), rel, t0, t1))
            if not todo:
                continue
            pos, skip = 0, 44      # byte position in the PCM (the 44-byte header is skipped)
            try:
                for chunk in fetcher.fetch(f["path"]):
                    if skip:
                        cut = min(skip, len(chunk))
                        chunk, skip = chunk[cut:], skip - cut
                    for s0, s1, w, *_ in todo:
                        lo, hi = max(s0, pos), min(s1, pos + len(chunk))
                        if hi > lo:
                            w.write(chunk[lo - pos:hi - pos])
                    pos += len(chunk)
            finally:
                for _, _, w, *_ in todo:
                    w.close()
            for s0, s1, w, rel, t0, t1 in todo:
                rows.append(C.make_row(
                    origin="negatives", session="negatives", source=src, device=d, session_device=d, speaker="",
                    split="unassigned", eval_channel=True, condition="household", label="negative", kind="negative",
                    clip=rel, start_utc=X.utc_str(t0), end_utc=X.utc_str(t1), source_run=run_id, revision=""))
            C.append_jsonl(manifest, rows[-len(todo):])
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
