#!/usr/bin/env python3
"""Owner-run, after a kivi_session.py session: collect the satellite's wake-stage audio, align it to
the desk mic, cut the labelled clips of all three sources and store everything in the permanent
dataset.

    kivi_extract.py --session-dir ~/kivi-sessions/<session id>            # reads the pod with kubectl
    kivi_extract.py --session-dir ... --dry-run                           # list what it would do
    kivi_extract.py --button-takes --device c3-b --speaker spk02 --split test --since ... --until ...

Steps
1. Pre-flight (read only): the wake word id of the Assist pipelines (`.storage/assist_pipeline.pipelines`).
   If the model under test (`kivi`) is the live wake word, a detection closes the satellite's wake file
   in the middle of the session; the tool handles that (see 3) and warns loudly.
2. List the satellite's `00_wake-*.wav` and `01_stt-*.wav` files that overlap the session window (the
   device id prefix comes from kivi_devices.json). A file's end is its listed mtime and its start is
   `mtime - (size - 44) / 32000` (about +-1 s). Only the session window +-60 s is copied, by byte
   range (`dd` in the pod: read only), never the whole multi-hour file, into `*.part` files that are
   renamed after the byte count was checked; a short copy fails. An open file has a placeholder
   header, so PCM is read from offset 44. Home Assistant deletes files after 90 days: run this soon.
3. Align every source to the desk mic (the reference clock) by GCC-PHAT in 15 s windows (all windows
   tried, ranked by the weaker signal), then a fine search and a linear fit. Files whose alignment is
   not trusted are listed prominently and left out (`--include-unaligned` to keep them, flagged).
   A closed wake file that ends inside a cue window is a server detection of the live wake word: the cue
   keeps its place in the counts, is flagged `run_end_in_window`, and its clip ends at the file end.
   Cues that no aligned audio covers are counted as `lost`, never dropped from the denominators.
4. Cut a clip per cue (cue - 0.5 s ... cue + 3.5 s, shortened before the next cue), the silence
   block and the gaps between cues (`background`), for `tv-<device>`, `pc-mic` and `webcam-mic`.
5. Write `<dataset>/<session>/<source>/{raw,raw-stt,clips,alignment.json}`; every clip is written
   atomically and its manifest row is appended (fsync per line) right after it. A re-run adds the
   rows of clips that exist but are missing from `manifest.jsonl`.

The dataset is permanent: this tool never deletes or overwrites anything (existing files are kept,
a re-run adds a revision), directories are mode 700 and files 600.
"""
import argparse
import glob
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kivi_real_common as C  # noqa: E402

RATE = C.RATE
KINDS = {"wake": "00_wake*.wav", "stt": "01_stt*.wav"}
CLOSED_AGE_S = 120.0     # a file not written for this long is closed (its run ended)
SAFE_PATH = re.compile(r"^/[A-Za-z0-9_./@=+-]+$")


# ---------------------------------------------------------------------------------------------
# where the wake files come from
# ---------------------------------------------------------------------------------------------


class LocalFetcher:
    """A directory laid out like /voice-recordings (tests and offline copies)."""

    def __init__(self, root):
        self.root = Path(root)

    def list_run_files(self, prefix, kind="wake"):
        out = []
        for p in sorted(glob.glob(str(self.root / f"{prefix}*" / "*" / "*" / KINDS[kind]))):
            st = os.stat(p)
            out.append({"path": p, "size": st.st_size, "mtime": st.st_mtime})
        return out

    def fetch_range(self, path, lo, hi):
        """Bytes [lo, hi) of a file."""
        with open(path, "rb") as f:
            f.seek(lo)
            left = hi - lo
            while left > 0:
                b = f.read(min(1 << 20, left))
                if not b:
                    return
                left -= len(b)
                yield b


class KubectlFetcher:
    """The HA pod, read only: one `sh -c` with a glob and `stat`, and `dd` per byte range."""

    def __init__(self, context, namespace, pod, container=None, root="/voice-recordings"):
        self.base = ["kubectl", "--context", context, "-n", namespace, "exec", pod]
        if container:
            self.base += ["-c", container]
        self.root = root

    def list_run_files(self, prefix, kind="wake"):
        assert re.fullmatch(r"[0-9a-f]{4,32}", prefix)
        script = (f'for f in {self.root}/{prefix}*/*/*/{KINDS[kind]}; do '
                  '[ -f "$f" ] && echo "$f $(stat -c "%s %Y" "$f")"; done')
        r = subprocess.run(self.base + ["--", "sh", "-c", script], capture_output=True, text=True, timeout=300)
        if r.returncode not in (0, 1):
            raise SystemExit(f"kubectl exec failed ({r.returncode}): {r.stderr.strip()[:300]}")
        out = []
        for line in r.stdout.splitlines():
            m = re.fullmatch(r"(\S+) (\d+) (\d+)", line.strip())
            if m and SAFE_PATH.match(m.group(1)):
                out.append({"path": m.group(1), "size": int(m.group(2)), "mtime": float(m.group(3))})
        return out

    def fetch_range(self, path, lo, hi):
        """Bytes [lo, hi) with `dd bs=4096 skip= count=` (seeks, works in BusyBox), trimmed here."""
        if not SAFE_PATH.match(path):
            raise SystemExit(f"refusing an unexpected path {path!r}")
        sk = lo // 4096
        cnt = -(-(hi - sk * 4096) // 4096)
        p = subprocess.Popen(self.base + ["--", "sh", "-c", f'dd if="$1" bs=4096 skip={sk} count={cnt} 2>/dev/null', "sh", path],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        skip, left = lo - sk * 4096, hi - lo
        while left > 0:
            b = p.stdout.read(1 << 20)
            if not b:
                break
            if skip:
                cut = min(skip, len(b))
                b, skip = b[cut:], skip - cut
            b = b[:left]
            left -= len(b)
            if b:
                yield b
        p.stdout.close()
        if p.wait() != 0:
            raise SystemExit(f"kubectl dd {path} failed: {p.stderr.read().decode()[:300]}")


def run_of(path):
    parts = Path(path).parts
    return {"pipeline": parts[-3], "run": parts[-2]}


def list_files(fetcher, prefix, kind="wake"):
    """Listed files with start = mtime - (size - 44) / 32000, the number of samples and the run."""
    out = []
    for f in fetcher.list_run_files(prefix, kind):
        n = max(0, (f["size"] - 44) // 2)
        f.update(n=n, dur=n / RATE, start=f["mtime"] - n / RATE, **run_of(f["path"]))
        out.append(f)
    return sorted(out, key=lambda f: f["start"])


def fetch_samples(fetcher, f, s0, s1, rawdir, tag=None):
    """Copy samples [s0, s1) of a listed file into `rawdir` as a proper WAV: through a `.part`
    file, with the byte count checked against the range, and cut at the listed size. Returns
    (path, name). Reuses a local copy of the same range."""
    s1 = min(s1, f["n"])
    b0, b1 = 44 + 2 * s0, 44 + 2 * s1
    if s1 <= s0 or b1 > f["size"]:
        raise SystemExit(f"bad range {s0}-{s1} for a file of {f['n']} samples")
    name = f"{tag or f['run']}@{s0}-{s1}.wav"
    dst = Path(rawdir) / name
    if dst.exists() and dst.stat().st_size == 44 + (b1 - b0):
        return dst, name
    if dst.exists():
        name = f"{tag or f['run']}@{s0}-{s1}-{time.time_ns()}.wav"
        dst = Path(rawdir) / name
    w = C.PartWavWriter(dst)
    try:
        for chunk in fetcher.fetch_range(f["path"], b0, b1):
            w.write(chunk)
        w.commit(expected_bytes=b1 - b0)
    except Exception as e:
        w.close()
        raise SystemExit(f"copy of {f['run']} failed ({e}); the partial file {w.part.name} was left in {rawdir}")
    return dst, name


def window_samples(f, lo_epoch, hi_epoch, pad):
    """Sample range of a listed file covering [lo - pad, hi + pad], and whether it reaches the file end."""
    lo_t, hi_t = max(f["start"], lo_epoch - pad), min(f["mtime"], hi_epoch + pad)
    s0 = max(0, int(math.floor((lo_t - f["start"]) * RATE)))
    s1 = min(f["n"], int(math.ceil((hi_t - f["start"]) * RATE)))
    return s0, s1, s1 >= f["n"]


# ---------------------------------------------------------------------------------------------
# sources as aligned streams
# ---------------------------------------------------------------------------------------------


class Stream:
    """One source: one or more files, each with an offset d(tau) = a + b * tau (source time =
    reference time + d), tau in seconds of the reference (pc-mic) file."""

    def __init__(self, label, ref_len):
        self.label, self.ref_len, self.files = label, ref_len, []

    def add(self, pcm, a, b, **info):
        self.files.append({"pcm": pcm, "a": a, "b": b, **info})

    @staticmethod
    def d(f, tau):
        return f["a"] + f["b"] * tau

    @staticmethod
    def coverage(f, ref_dur):
        """Reference-time interval that the file's audio spans (clipped to the reference recording)."""
        lo = max(0.0, (0.0 - f["a"]) / (1 + f["b"]))
        hi = min(ref_dur, (len(f["pcm"]) / RATE - f["a"]) / (1 + f["b"]))
        return (lo, hi) if hi > lo else None

    def cut(self, tau0, tau1, min_cov=0.9):
        n = int(round((tau1 - tau0) * RATE))
        mid = (tau0 + tau1) / 2
        best = None
        for f in self.files:
            j0 = int(round((tau0 + self.d(f, mid)) * RATE))
            valid = max(0, min(len(f["pcm"]), j0 + n) - max(0, j0))
            if best is None or valid > best[0]:
                best = (valid, j0, f)
        if best is None or n <= 0 or best[0] / n < min_cov:
            return None
        valid, j0, f = best
        out = np.zeros(n, np.int16)
        a, b = max(0, j0), min(len(f["pcm"]), j0 + n)
        out[a - j0:b - j0] = f["pcm"][a:b]
        return out, self.d(f, mid), f


def parse_utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def utc_str(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def copy_new(src, dst):
    """Copy a file into the dataset through a `.part` file; never overwrites. False if `dst` exists."""
    if Path(dst).exists():
        return False
    part = C.part_path(dst)
    with open(src, "rb") as fi, C.create_exclusive(part) as fo:
        while True:
            b = fi.read(1 << 20)
            if not b:
                break
            fo.write(b)
        fo.flush()
        os.fsync(fo.fileno())
    C.finalize_part(part, dst)
    return True


def parse_when(text):
    """ISO time to epoch seconds. A time without an offset is local time."""
    d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return d.timestamp() if d.tzinfo else d.astimezone().timestamp()


class FilePresses:
    """Button presses from a JSON list of ISO times (tests, or a copy of the query result)."""

    def __init__(self, path):
        self.times = sorted(parse_when(t) for t in json.loads(Path(path).read_text(encoding="utf-8")))

    def presses(self, since, until):
        return [t for t in self.times if since <= t <= until]


class PostgresPresses:
    """Presses of `input_button.kivi_take` from Home Assistant's recorder database, with a single
    read-only SELECT through `kubectl exec ... psql` (default_transaction_read_only on, and the
    statement is checked to be a SELECT). The state of an input_button is the time of its last press,
    so the press times are the DISTINCT state values: a state restored after a Home Assistant restart
    is a new row with the same value and is not a new press."""

    def __init__(self, context, namespace, pod, container, user, db, entity="input_button.kivi_take"):
        self.base = ["kubectl", "--context", context, "-n", namespace, "exec", pod]
        if container:
            self.base += ["-c", container]
        self.user, self.db, self.entity = user, db, entity
        if not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", entity):
            raise SystemExit(f"bad entity id {entity!r}")

    def presses(self, since, until):
        sql = ("SELECT DISTINCT s.state FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id "
               f"WHERE m.entity_id = '{self.entity}' AND s.state ~ '^20[0-9][0-9]-' "
               f"AND s.last_updated_ts >= {float(since) - 60} ORDER BY 1")
        assert sql.lstrip().upper().startswith("SELECT") and ";" not in sql
        cmd = self.base + ["--", "env", "PGOPTIONS=-c default_transaction_read_only=on",
                           "psql", "-U", self.user, "-d", self.db, "-At", "-c", sql]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise SystemExit(f"reading the button presses failed: {r.stderr.strip()[:300]}\n"
                             "Use --presses-file with a JSON list of ISO times instead.")
        return state_press_times(r.stdout.splitlines(), since, until)


def state_press_times(states, since, until):
    """Press times (epoch) from input_button state strings, de-duplicated and inside the window."""
    out = set()
    for line in states:
        line = line.strip()
        if not line:
            continue
        try:
            t = parse_when(line)
        except ValueError:
            continue
        if since <= t <= until:
            out.add(round(t, 3))
    return sorted(out)


def summarize_alignment(label, run, al, included=True):
    return {"source": label, "run": run, "ok": al["ok"], "included": included, "offset_a_s": round(al["a"], 4),
            "prior_s": round(al["d_prior_s"], 4), "drift_ppm": al["drift_ppm"],
            "drift_over_session_s": al["drift_over_session_s"], "windows": al["n_windows"],
            "coarse_windows": al.get("n_coarse"), "confident": al["n_confident"], "inliers": al["n_inliers"],
            "median_conf": round(al["median_conf"], 1), "resid_std_ms": al["resid_std_ms"]}


def preflight(a, device, fetcher_is_local):
    """Print which wake word the pipelines use; loud warning if it is the model under test."""
    if a.no_preflight or (fetcher_is_local and not a.pipelines_file):
        return None
    text = Path(a.pipelines_file).read_text(encoding="utf-8") if a.pipelines_file else None
    r = C.pipeline_preflight(a.context, a.namespace, a.pod, a.container, a.model_name, text)
    if "error" in r:
        print(f"pre-flight: could not read the pipelines ({r['error']}); continuing")
    else:
        print(f"pre-flight: the preferred Assist pipeline's wake word is {r['preferred_wake_word']!r} "
              f"(others: {', '.join(r['others']) or 'none'}; {device} is assumed to use the preferred pipeline)")
        if r["live"] or r["any_live"]:
            print("*" * 78 + f"\n* WARNING: {a.model_name!r} is a live wake word. A detection closes the satellite's wake\n"
                  "* file mid-session: those cues are counted as server detections (run_end_in_window)\n"
                  "* and the audio after them is lost until the next run starts.\n" + "*" * 78)
    return r


# ---------------------------------------------------------------------------------------------
# extraction of a kivi_session.py session
# ---------------------------------------------------------------------------------------------


def extract(a, fetcher=None):
    sdir = Path(a.session_dir).expanduser()
    session = json.loads((sdir / "session.json").read_text(encoding="utf-8"))
    sid, device = session["session_id"], session["device"]
    tv = session.get("tv_source") or C.source_label(device)
    if session.get("status") != "complete":
        print(f"WARNING: session status is {session.get('status')!r}; extracting what exists.")
    o_pc = C.session_pc_offset(session)
    o_web = float(session["sources"]["webcam-mic"]["first_sample_offset_s"])
    t0 = parse_utc(session["t0_utc"]).timestamp()
    t_end = t0 + float(session["end_offset_s"])
    pc = C.read_pcm16(sdir / "pc-mic.wav")
    web = C.read_pcm16(sdir / "webcam-mic.wav")
    ref_dur = len(pc) / RATE
    now = time.time() if a.now is None else a.now
    exid = "ex-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    rev = a.revision or ""
    kw = dict(coarse_lag_s=a.coarse_lag_s, refine_lag_s=a.refine_lag_s, win_s=a.win_s, min_conf=a.min_conf)
    pre = None
    tv_files, stt_files = [], []
    if not a.no_tv:
        prefix = C.device_prefix(device, a.devices_file, a.device_id_prefix)
        is_local = fetcher is not None
        fetcher = fetcher or KubectlFetcher(a.context, a.namespace, a.pod, a.container, a.recordings_root)
        pre = preflight(a, device, is_local)
        keep = lambda f: f["mtime"] >= t0 - a.slack and f["start"] <= t_end + a.slack  # noqa: E731
        tv_files = [f for f in list_files(fetcher, prefix, "wake") if keep(f)]
        runs = {(f["pipeline"], f["run"]) for f in tv_files}
        stt_files = [f for f in list_files(fetcher, prefix, "stt") if (f["pipeline"], f["run"]) in runs]
        print(f"{tv}: {len(tv_files)} wake file(s) and {len(stt_files)} request-audio file(s) overlap the session "
              f"window (device {device}, id prefix {prefix}); session {ref_dur / 60:.1f} min")
        if not tv_files:
            print("WARNING: no wake file overlaps the session. Was the satellite unmuted and connected? "
                  "Continuing with the PC sources only.")
    if a.dry_run:
        for f in tv_files:
            s0, s1, _ = window_samples(f, t0, t_end, a.copy_pad_s)
            print(f"  would copy {f['pipeline']}/{f['run']}: {(s1 - s0) / RATE:.0f} s of {f['dur']:.0f} s, "
                  f"file ends {datetime.fromtimestamp(f['mtime'], timezone.utc):%H:%M:%SZ}")
        print(f"  would cut {len(C.cue_windows(session, a.pad_before, a.pad_after))} cue clips and "
              f"the non-cue segments per source into {a.dataset_root}/{sid}")
        return None

    root = Path(a.dataset_root).expanduser()
    C.private_mkdir(root, root)
    sroot = C.private_mkdir(root / sid, root)
    manifest = root / "manifest.jsonl"
    summaries = []
    include_bad = a.include_unaligned

    # reference + webcam
    for label, src in (("pc-mic", "pc-mic.wav"), ("webcam-mic", "webcam-mic.wav")):
        raw = C.private_mkdir(sroot / label / "raw", root)
        copy_new(sdir / src, raw / src)
    streams = {"pc-mic": Stream("pc-mic", len(pc))}
    streams["pc-mic"].add(pc, 0.0, 0.0, run="", name="pc-mic.wav", ok=True, conf=None, drift_ppm=0.0, run_end=None)
    records = {"pc-mic": [{"name": "pc-mic.wav", "run": "", "ok": True, "included": True, "offset_a_s": 0.0, "b": 0.0,
                           "drift_ppm": 0.0, "median_conf": None, "coverage_s": [0.0, round(ref_dur, 3)],
                           "run_end_ref_s": None, "windows": []}]}
    al = C.align_stream(pc, web, o_pc - o_web, coarse_lag_s=2.0, refine_lag_s=0.5, win_s=a.win_s, min_conf=a.min_conf)
    inc = al["ok"] or include_bad
    summaries.append(summarize_alignment("webcam-mic", "", al, inc))
    streams["webcam-mic"] = Stream("webcam-mic", len(pc))
    cov = Stream.coverage({"a": al["a"], "b": al["b"], "pcm": web}, ref_dur)
    if inc:
        streams["webcam-mic"].add(web, al["a"], al["b"], run="", name="webcam-mic.wav", ok=al["ok"], conf=al["median_conf"],
                                  drift_ppm=al["drift_ppm"], align=al, run_end=None)
    records["webcam-mic"] = [{"name": "webcam-mic.wav", "run": "", "ok": bool(al["ok"]), "included": bool(inc),
                              "offset_a_s": round(al["a"], 5), "b": al["b"], "drift_ppm": al["drift_ppm"],
                              "median_conf": al["median_conf"], "coverage_s": [round(x, 3) for x in cov] if cov else None,
                              "run_end_ref_s": None, "windows": al["windows"]}]

    # satellite: the session window +-copy_pad_s of every overlapping file, by byte range
    if tv_files:
        streams[tv] = Stream(tv, len(pc))
        records[tv] = []
        raw = C.private_mkdir(sroot / tv / "raw", root)
        for f in tv_files:
            s0, s1, reaches_end = window_samples(f, t0, t_end, a.copy_pad_s)
            if s1 - s0 < 5 * RATE:
                continue
            dst, name = fetch_samples(fetcher, f, s0, s1, raw)
            sig = C.read_pcm16(dst)
            s_est = (f["start"] + s0 / RATE) - t0     # session time of the copy's first sample (listed size and mtime)
            al = C.align_stream(pc, sig, o_pc - s_est, **kw)
            inc = al["ok"] or include_bad
            summaries.append(summarize_alignment(tv, f["run"], al, inc))
            closed = reaches_end and now - f["mtime"] >= CLOSED_AGE_S
            fd = {"pcm": sig, "a": al["a"], "b": al["b"]}
            cov = Stream.coverage(fd, ref_dur)
            run_end = (len(sig) / RATE - al["a"]) / (1 + al["b"]) if closed else None
            if inc:
                streams[tv].add(sig, al["a"], al["b"], run=f"{f['pipeline']}/{f['run']}", name=name, ok=al["ok"],
                                conf=al["median_conf"], drift_ppm=al["drift_ppm"], align=al, run_end=run_end)
            records[tv].append({"name": name, "run": f"{f['pipeline']}/{f['run']}", "ok": bool(al["ok"]), "included": bool(inc),
                                "offset_a_s": round(al["a"], 5), "b": al["b"], "drift_ppm": al["drift_ppm"],
                                "median_conf": al["median_conf"], "coverage_s": [round(x, 3) for x in cov] if cov else None,
                                "run_end_ref_s": round(run_end, 3) if run_end is not None else None,
                                "copied_from_s": s0 / RATE, "listed_samples": f["n"], "windows": al["windows"]})
        sdirs = C.private_mkdir(sroot / tv / "raw-stt", root)
        for f in stt_files:
            if f["n"] > 0:
                fetch_samples(fetcher, f, 0, f["n"], sdirs, tag=f["run"])

    # alignment records (the scorer reads these)
    report_streams = {}
    for label, recs in records.items():
        aname = "alignment.json" if not (sroot / label / "alignment.json").exists() else f"alignment-{exid}.json"
        C.write_json_new(sroot / label / aname, {"extract_id": exid, "source": label, "reference": "pc-mic",
                                                 "ref_samples": len(pc), "pc_offset_s": o_pc, "files": recs})
        report_streams[label] = {"alignment": aname, "files": len(recs), "included": sum(1 for r in recs if r["included"])}

    # clips
    windows = C.cue_windows(session, a.pad_before, a.pad_after)
    segs = C.noncue_segments(session, windows)
    blocks = {b["index"]: b for b in session["blocks"]}
    done = {(r["source"], r["clip"]) for r in C.read_jsonl(manifest) if r["session"] == sid}
    cdir = "clips" + (f"-{C.ascii_slug(rev)}" if rev else "")
    items = [("cue", w["block"], w["cue"], w["label"], w["word"], w["t0"], w["t1"], w["cue_offset_s"]) for w in windows]
    for n, (bi, lab, t0_, t1_) in enumerate(segs, 1):
        items.append(("seg", bi, n, lab, "", t0_, t1_, None))
    stats = {}
    rows_written = 0
    for label, st in streams.items():
        s = stats.setdefault(label, {"cues": 0, "cues_cut": 0, "lost_cues": 0, "run_end_in_window": 0,
                                     "segments_uncovered": 0, "rows_reconciled": 0})
        for kind, bi, idx, lab, word, t0_, t1_, cue_off in items:
            b = blocks[bi]
            tau0, tau1 = t0_ - o_pc, t1_ - o_pc
            run_end = None
            if kind == "cue":
                s["cues"] += 1
                ends = [f["run_end"] for f in st.files if f.get("run_end") is not None and tau0 <= f["run_end"] < tau1]
                if ends:
                    run_end = min(ends)
                    tau1 = max(tau0, run_end)
                    s["run_end_in_window"] += 1
            if tau1 - tau0 < (1.0 if run_end is not None else 0.1):
                got = None
            else:
                got = st.cut(tau0, tau1)
            if got is None:
                s["lost_cues" if kind == "cue" else "segments_uncovered"] += 1
                continue
            pcm, d_used, f = got
            stem = f"{'c' if kind == 'cue' else 'g'}{idx:03d}-{C.ascii_slug(lab)}"
            rel = f"{sid}/{label}/{cdir}/{bi:02d}-{C.ascii_slug(b['name'])}/{stem}.wav"
            row = C.make_row(
                origin="session", session=sid, source=label, device=device if label == tv else label,
                session_device=device, speaker=session.get("speaker", ""),
                split=C.channel_split(session.get("split", "unassigned"), label), eval_channel=C.is_eval_channel(label),
                condition=C.condition_string(b), conditions=b.get("conditions", {}), tags=session.get("tags", {}),
                block=bi, block_name=b["name"], label=lab, word=word, kind="cue" if kind == "cue" else "segment",
                clip=rel, start_s=round(t0_, 3), end_s=round(t0_ + (tau1 - tau0), 3),
                start_utc=utc_str(t0_ + t0), end_utc=utc_str(t0_ + (tau1 - tau0) + t0),
                cue_index=idx if kind == "cue" else None, cue_offset_s=cue_off,
                align_offset_s=round(d_used, 5), align_conf=f["conf"], align_ok=bool(f["ok"]),
                drift_ppm=f["drift_ppm"], source_run=f["run"], revision=rev, extract_id=exid,
                dry_run_session=bool(session.get("dry_run")), run_end_in_window=run_end is not None, needs_review=False)
            C.private_mkdir(root / Path(rel).parent, root)
            if (label, rel) in done:
                s["cues_cut"] += kind == "cue"
                continue
            if not (root / rel).exists():
                C.write_wav_new(root / rel, pcm)
            else:
                s["rows_reconciled"] += 1       # on disk but missing from the manifest (a crash): add its row
            C.append_jsonl(manifest, [row])     # one fsynced line per clip, right after the clip
            rows_written += 1
            s["cues_cut"] += kind == "cue"
    if not (sroot / "session.json").exists():
        C.write_json_new(sroot / "session.json", session)
    report = {"extract_id": exid, "session": sid, "device": device, "revision": rev, "alignment": summaries,
              "preflight": pre, "rows_written": rows_written, "per_source": stats, "streams": report_streams}
    C.write_json_new(sroot / f"extract-{exid}.json", report)

    print(f"\nWrote {rows_written} manifest rows (clips) to {sroot}; manifest {manifest}")
    for s in summaries:
        flag = "OK " if s["ok"] else "NOT TRUSTED"
        used = "" if s["ok"] else (" (kept: --include-unaligned)" if s["included"] else " (LEFT OUT of clips and scores)")
        print(f"  align {s['source']:11s} {s['run'][:12]:12s} {flag}{used}: offset {s['offset_a_s']:+.3f} s "
              f"(prior {s['prior_s']:+.3f}), {s['confident']}/{s['windows']} windows confident, median PSR "
              f"{s['median_conf']}, drift {s['drift_ppm']:+.1f} ppm = {s['drift_over_session_s'] * 1000:+.0f} ms, "
              f"residual {s['resid_std_ms']} ms")
    for label, s in stats.items():
        print(f"  {label:11s} cues {s['cues']}: cut {s['cues_cut']}, lost or partly uncovered {s['lost_cues']}, "
              f"server run end inside the window {s['run_end_in_window']}"
              + (f", {s['rows_reconciled']} manifest rows reconciled" if s["rows_reconciled"] else ""))
    if any(not s["ok"] for s in summaries):
        print("  WARNING: a source is not aligned reliably. Check the TV was audible to the desk mic (TV on, you "
              "speaking) or raise --coarse-lag-s; or pass --include-unaligned to keep it, flagged.")
    return report


# ---------------------------------------------------------------------------------------------
# button takes
# ---------------------------------------------------------------------------------------------


def button_takes(a, fetcher=None, presses=None):
    """Takes marked by presses of `input_button.kivi_take`: for each press, find the word in the
    wake audio of the device within 0-6 s after it (energy only), and cut a clip around it. Takes with
    more than one candidate burst are flagged `needs_review` and kept out of `test` unless
    `--keep-review-in-split`."""
    C.valid_speaker(a.speaker)
    if a.split not in C.SPLITS:
        raise SystemExit(f"--split must be one of {C.SPLITS}")
    device = C.valid_device(a.device)
    tv = C.source_label(device)
    since, until = parse_when(a.since), parse_when(a.until)
    if until <= since:
        raise SystemExit("--until must be after --since")
    presses = presses or (FilePresses(a.presses_file) if a.presses_file else
                          PostgresPresses(a.context, a.namespace, a.pg_pod, a.pg_container, a.pg_user, a.pg_db))
    times = presses.presses(since, until)
    prefix = C.device_prefix(device, a.devices_file, a.device_id_prefix)
    is_local = fetcher is not None
    fetcher = fetcher or KubectlFetcher(a.context, a.namespace, a.pod, a.container, a.recordings_root)
    preflight(a, device, is_local)
    files = [f for f in list_files(fetcher, prefix, "wake") if f["mtime"] >= since - a.slack and f["start"] <= until + a.slack]
    print(f"{len(times)} button press(es) between {a.since} and {a.until}; {len(files)} {tv} wake file(s) overlap")
    if a.dry_run:
        return None
    root = Path(a.dataset_root).expanduser()
    sid = f"button-{datetime.fromtimestamp(since, timezone.utc):%Y%m%dT%H%M%SZ}-{a.speaker}-{tv}"
    sroot = C.private_mkdir(root / sid, root)
    raw = C.private_mkdir(sroot / tv / "raw", root)
    loaded = []
    for f in files:
        s0, s1, _ = window_samples(f, since, until, a.copy_pad_s)
        if s1 - s0 < RATE:
            continue
        dst, _ = fetch_samples(fetcher, f, s0, s1, raw)
        loaded.append((f, f["start"] + s0 / RATE, C.read_pcm16(dst)))
    exid = "ex-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    manifest = root / "manifest.jsonl"
    done = {r["clip"] for r in C.read_jsonl(manifest) if r["session"] == sid}
    stats = {"presses": len(times), "no_file": 0, "no_clear_onset": 0, "clips": 0, "needs_review": 0,
             "already_there": 0, "reconciled": 0}
    after = []
    tol = a.tolerance_s
    for n, t in enumerate(times, 1):
        hit = next(((f, b0, x) for f, b0, x in loaded if b0 - tol <= t <= f["mtime"] + tol), None)
        if hit is None:
            stats["no_file"] += 1
            continue
        f, base, x = hit
        dur = len(x) / RATE
        rel = t - base
        c0, c1 = max(0.0, rel - 10.0), min(dur, rel + a.window_s + tol + 10.0)
        segs = C.energy_segments(x[int(c0 * RATE):int(c1 * RATE)], c0, margin_db=a.margin_db,
                                 min_len_s=0.15, max_len_s=a.max_word_s, merge_gap_s=0.3)
        cands = [s for s in segs if not s["long"] and rel - tol <= s["t0"] <= rel + a.window_s + tol
                 and s["snr_db"] >= a.min_snr_db]
        if not cands:
            stats["no_clear_onset"] += 1
            continue
        w = max(cands, key=lambda s: s["peak_db"])
        k0, k1 = max(0.0, w["t0"] - a.pad), min(dur, w["t1"] + a.pad)
        clip = f"{sid}/{tv}/clips/00-button-takes/p{n:03d}-kivi.wav"
        review = len(cands) > 1
        split = a.split
        if review and split == "test" and not a.keep_review_in_split:
            split = "unassigned"
        if review:
            stats["needs_review"] += 1
        row = C.make_row(
            origin="button", session=sid, source=tv, device=device, session_device=device, speaker=a.speaker,
            split=split, split_requested=a.split, eval_channel=True, condition="button-take", conditions={}, tags={},
            block=1, block_name="button-takes", label="kivi", word="kivi", kind="cue", clip=clip,
            start_s=round(base + k0 - since, 3), end_s=round(base + k1 - since, 3),
            start_utc=utc_str(base + k0), end_utc=utc_str(base + k1), cue_index=n,
            source_run=f"{f['pipeline']}/{f['run']}", revision="", extract_id=exid, press_utc=utc_str(t),
            onset_after_press_s=round(w["t0"] - rel, 2), snr_db=w["snr_db"], n_candidates=len(cands),
            needs_review=review)
        if clip in done:
            stats["already_there"] += 1
            continue
        C.private_mkdir(root / Path(clip).parent, root)
        if (root / clip).exists():
            stats["reconciled"] += 1
        else:
            C.write_wav_new(root / clip, x[int(k0 * RATE):int(k1 * RATE)])
        C.append_jsonl(manifest, [row])
        after.append(w["t0"] - rel)
        stats["clips"] += 1
    meta = sroot / "session.json"
    if not meta.exists():
        C.write_json_new(meta, {"schema": 1, "session_id": sid, "kind": "button-takes", "device": device,
                                "tv_source": tv, "speaker": a.speaker, "split": a.split, "since": utc_str(since),
                                "until": utc_str(until), "t0_utc": utc_str(since), "blocks": []})
    C.write_json_new(sroot / f"extract-{exid}.json", {"extract_id": exid, **stats,
                     "median_onset_after_press_s": round(float(np.median(after)), 2) if after else None})
    print(f"clips cut: {stats['clips']} of {stats['presses']} presses; no wake file for {stats['no_file']}; "
          f"no clear speech onset for {stats['no_clear_onset']}; flagged needs_review (several candidate bursts) "
          f"{stats['needs_review']}; already in the dataset {stats['already_there']}"
          + (f"; median onset {np.median(after):.1f} s after the press" if after else ""))
    if stats["no_clear_onset"]:
        print("  (a press without a clear onset means no burst of speech 10 dB above the room within the window; "
              "check the satellite was unmuted and the take was spoken within 6 s of the press)")
    if stats["needs_review"] and a.split == "test" and not a.keep_review_in_split:
        print("  needs_review takes were set to split 'unassigned' (not test, not train) until the owner decides")
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--session-dir", help="a kivi_session.py directory (not with --button-takes)")
    ap.add_argument("--button-takes", action="store_true",
                    help="cut takes marked by input_button.kivi_take presses: needs --device --speaker --split --since --until")
    ap.add_argument("--device", default="c3-b", help="(--button-takes) the satellite the takes were spoken to")
    ap.add_argument("--speaker", help="(--button-takes) pseudonymous id, e.g. spk02")
    ap.add_argument("--split", choices=C.SPLITS, help="(--button-takes) required: train, dev, test, train-only or unassigned")
    ap.add_argument("--keep-review-in-split", action="store_true", help="(--button-takes) keep needs_review takes in --split")
    ap.add_argument("--since", help="(--button-takes) ISO time; without an offset it is local time")
    ap.add_argument("--until", help="(--button-takes) ISO time")
    ap.add_argument("--presses-file", help="(--button-takes) JSON list of ISO press times instead of the database")
    ap.add_argument("--pg-pod", default="home-assistant-postgres-0")
    ap.add_argument("--pg-container", default="postgres")
    ap.add_argument("--pg-user", default="postgres")
    ap.add_argument("--pg-db", default="home_assistant")
    ap.add_argument("--window-s", type=float, default=6.0, help="(--button-takes) look for the word this long after a press")
    ap.add_argument("--tolerance-s", type=float, default=1.0, help="(--button-takes) timing uncertainty of the file (mtime)")
    ap.add_argument("--pad", type=float, default=1.0, help="(--button-takes) padding around the word")
    ap.add_argument("--max-word-s", type=float, default=2.5)
    ap.add_argument("--min-snr-db", type=float, default=10.0)
    ap.add_argument("--margin-db", type=float, default=10.0)
    ap.add_argument("--dataset-root", default=C.DEFAULT_DATASET_ROOT)
    ap.add_argument("--devices-file")
    ap.add_argument("--device-id-prefix", help="override the prefix from kivi_devices.json")
    ap.add_argument("--context", default="grigri")
    ap.add_argument("--namespace", default="home-assistant")
    ap.add_argument("--pod", default="home-assistant-hass-0")
    ap.add_argument("--container")
    ap.add_argument("--recordings-root", default="/voice-recordings")
    ap.add_argument("--from-dir", help="read wake files from a local copy laid out like /voice-recordings")
    ap.add_argument("--model-name", default="kivi", help="the model under test, for the live-wake-word warning")
    ap.add_argument("--pipelines-file", help="a copy of assist_pipeline.pipelines instead of reading the pod")
    ap.add_argument("--no-preflight", action="store_true")
    ap.add_argument("--slack", type=float, default=30.0, help="seconds around the session to accept wake files")
    ap.add_argument("--copy-pad-s", type=float, default=60.0, help="copy the session window +- this much of each file")
    ap.add_argument("--pad-before", type=float, default=0.5)
    ap.add_argument("--pad-after", type=float, default=3.5)
    ap.add_argument("--coarse-lag-s", type=float, default=20.0)
    ap.add_argument("--refine-lag-s", type=float, default=1.5)
    ap.add_argument("--win-s", type=float, default=15.0)
    ap.add_argument("--min-conf", type=float, default=8.0, help="minimum PSR of a trusted alignment window")
    ap.add_argument("--include-unaligned", action="store_true",
                    help="keep files whose alignment is not trusted (flagged align_ok false)")
    ap.add_argument("--revision", help="tag for a re-extraction with other settings (clips-<tag>/)")
    ap.add_argument("--no-tv", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", type=float, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    fetcher = LocalFetcher(a.from_dir) if a.from_dir else None
    if a.button_takes:
        if not (a.speaker and a.since and a.until and a.split):
            raise SystemExit("--button-takes needs --speaker, --split, --since and --until")
        button_takes(a, fetcher)
    else:
        if not a.session_dir:
            raise SystemExit("--session-dir is required (or use --button-takes)")
        extract(a, fetcher)


if __name__ == "__main__":
    main()
