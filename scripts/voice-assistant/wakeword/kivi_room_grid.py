#!/usr/bin/env python3
"""Server grid of room pieces copied before the manifest recorded `src_sample0` (homelab 41f4a1bb4, 2026-11-12).

The wake word server cuts the audio of one wake file in 1280-sample steps from the file's sample 0. A room piece
(`kivi_negatives.py`) starts somewhere inside its wake file, so a replay must start its steps at piece sample
(-src_sample0) % 1280 (`kivi_replay_mine.grid_phase`). Old manifests have only the piece's start and end times, printed
to the millisecond (16 samples), so the offset is derived from the listed wake file (size and whole-second mtime give its
sample count N and its start) and the copy rules of `kivi_negatives.plan_file`:

- start-trim: the piece starts --trim-start-s (0.3 s, sample 4800) into the file;
- start-followup: it starts --boundary-s (7 s, sample 112000) into a follow-up file;
- end-boundary: it ends --boundary-s before the end of the file (s1 = N - 112000), so s0 = s1 - piece samples;
- start-midnight / end-midnight: a cut at Europe/Madrid midnight (a whole second), exact;
- bracketed: both edges at other cuts (a press or cue, +-10 s; a session window): only the 17-19 offsets that print as the
  manifest's milliseconds are possible; `lo` and `hi` are written, `src_sample0` stays null;
- inconsistent / no-listing: the listed file does not fit the piece (rewritten or gone): nothing is written but the method.

    kivi_room_grid.py derive --set negatives-v5era --set negatives-daily [--listing L.json] [--save-listing L.json] [--dry-run]

Writes `<dataset>/<set>/grid.jsonl` (one row per old piece: clip, src_sample0 or null, lo, hi, method; private), which
`kivi_replay_mine.read_pieces` reads. Pieces whose manifest row has `src_sample0` are skipped. The listing is read from the
Home Assistant pod read-only (`stat` of the wake files, as kivi_negatives.py does), or from a saved listing. Numbers only.
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kivi_extract as X  # noqa: E402
import kivi_real_common as C  # noqa: E402

RATE = C.RATE
TRIM_S0 = int(0.3 * RATE)          # kivi_negatives --trim-start-s default
BOUNDARY = int(7.0 * RATE)         # kivi_negatives --boundary-s default (follow-up start and closed-file end)
TZ = ZoneInfo(C.DAY_TZ)


def utc_ms(text):
    """Epoch milliseconds (int) of a manifest time printed by kivi_extract.utc_str."""
    d = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return int(d.replace(microsecond=0).timestamp()) * 1000 + d.microsecond // 1000


def madrid_midnight(ms):
    if ms % 1000:
        return False
    d = datetime.fromtimestamp(ms // 1000, TZ)
    return (d.hour, d.minute, d.second) == (0, 0, 0)


def possible(ms, file_start):
    """Sample offsets s whose time file_start + s / RATE prints as `ms` (utc_str truncates to the millisecond), with one
    sample of slack each side for the rounding of the copy (`round`) and of the microseconds. file_start in samples."""
    lo = ms * 16 - file_start
    return range(lo - 1, lo + 18)


def derive(start_utc, end_utc, n_piece, n_file, mtime):
    """{src_sample0, lo, hi, method} of one piece of a wake file with `n_file` samples, last written at whole-second
    epoch `mtime` (pure). src_sample0 is set only when exact."""
    fs = int(round(mtime)) * RATE - n_file            # the file start in samples since the epoch (exact)
    t0, t1 = utc_ms(start_utc), utc_ms(end_utc)
    cand = set(possible(t0, fs)) & {s - n_piece for s in possible(t1, fs)}
    cand = {s for s in cand if 0 <= s and s + n_piece <= n_file}
    if not cand:
        return {"src_sample0": None, "lo": None, "hi": None, "method": "inconsistent"}
    exact = [(TRIM_S0, "start-trim"), (BOUNDARY, "start-followup")]
    if t1 % 1000 == 0 and t1 // 1000 == int(round(mtime)) - BOUNDARY // RATE:
        exact.append((n_file - BOUNDARY - n_piece, "end-boundary"))
    if madrid_midnight(t0):
        exact.append((t0 * 16 - fs, "start-midnight"))
    if madrid_midnight(t1):
        exact.append((t1 * 16 - fs - n_piece, "end-midnight"))
    for s0, method in exact:
        if s0 in cand:
            return {"src_sample0": s0, "lo": s0, "hi": s0, "method": method}
    return {"src_sample0": None, "lo": min(cand), "hi": max(cand), "method": "bracketed"}


def listing_from_pod(a):
    f = X.KubectlFetcher(a.context, a.namespace, a.pod, a.container, a.recordings_root)
    out = {}
    for d in C.load_devices(a.devices_file):
        out[d] = [{"pipeline": x["pipeline"], "run": x["run"], "size": x["size"], "mtime": x["mtime"]}
                  for x in X.list_files(f, C.device_prefix(d, a.devices_file), "wake")]
    return out


def derive_set(root, set_name, files):
    """[(row, piece)] for every old piece of a set; `files` = {(device, 'pipeline/run'): {size, mtime}}."""
    rows = []
    for r in C.read_jsonl(Path(root) / set_name / "manifest.jsonl"):
        if r.get("src_sample0") is not None:
            continue
        p = Path(root) / r["clip"]
        if not p.exists():
            continue
        n_piece = max(0, (p.stat().st_size - 44) // 2)
        f = files.get((r["device"], r["source_run"]))
        if f is None:
            g = {"src_sample0": None, "lo": None, "hi": None, "method": "no-listing"}
        else:
            g = derive(r["start_utc"], r["end_utc"], n_piece, (f["size"] - 44) // 2, f["mtime"])
        rows.append({"clip": r["clip"], **g})
    return rows


def write_grid(path, rows):
    tmp = Path(str(path) + ".part")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def cmd_derive(a, out=print):
    root = Path(a.dataset_root).expanduser()
    if a.listing:
        listing = json.loads(Path(a.listing).read_text())
        listing = listing.get("files", listing)
    else:
        listing = listing_from_pod(a)
        if a.save_listing:
            Path(a.save_listing).write_text(json.dumps({"listed_utc": X.utc_str(datetime.now(timezone.utc).timestamp()),
                                                        "files": listing}))
            os.chmod(a.save_listing, 0o600)
    files = {(d, f"{x['pipeline']}/{x['run']}"): x for d, xs in listing.items() for x in xs}
    res = {}
    for st in a.set:
        rows = derive_set(root, st, files)
        cnt = Counter(r["method"] for r in rows)
        out(f"{st}: {len(rows)} old piece(s): " + ", ".join(f"{k} {v}" for k, v in sorted(cnt.items())))
        if not a.dry_run and rows:
            write_grid(root / st / "grid.jsonl", rows)
        res[st] = rows
    if a.dry_run:
        out("dry run: nothing written")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["derive"])
    ap.add_argument("--dataset-root", default=C.DEFAULT_DATASET_ROOT)
    ap.add_argument("--set", action="append", required=True)
    ap.add_argument("--listing", help="saved listing JSON instead of the pod")
    ap.add_argument("--save-listing", help="write the pod listing here (private)")
    ap.add_argument("--devices-file")
    ap.add_argument("--context", default="grigri")
    ap.add_argument("--namespace", default="home-assistant")
    ap.add_argument("--pod", default="home-assistant-hass-0")
    ap.add_argument("--container")
    ap.add_argument("--recordings-root", default="/voice-recordings")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    cmd_derive(a)


if __name__ == "__main__":
    main()
