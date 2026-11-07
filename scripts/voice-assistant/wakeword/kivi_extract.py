#!/usr/bin/env python3
"""Owner-run, after a kivi_session.py session: collect the satellite's wake-stage audio, align it to
the desk mic, cut the labelled clips of all three sources and store everything in the permanent
dataset.

    kivi_extract.py --session-dir ~/kivi-sessions/<session id>            # reads the pod with kubectl
    kivi_extract.py --session-dir ... --dry-run                           # list what it would do

Steps
1. Find the satellite's `00_wake-*.wav` files that overlap the session window (the device id prefix
   comes from kivi_devices.json: `--device` of the session) and copy them out with
   `kubectl exec ... cat` (read-only; stat and cat only). A file's end time is its mtime and its start
   is mtime - duration (about +-1 s); an open file has a placeholder header, so PCM is read from
   offset 44. Home Assistant deletes files after 90 days: run this soon after the session.
2. Align every source to the desk mic (the reference clock) by GCC-PHAT cross-correlation in 15 s
   windows: a coarse search around the mtime estimate, then a fine search and a linear fit. The
   alignment confidence (peak-to-sidelobe ratio) and the drift over the session are printed and stored.
3. Cut a clip per cue (cue - 0.5 s ... cue + 3.5 s, shortened before the next cue), the silence
   block and the gaps between cues (`background`), for `tv-<device>`, `pc-mic` and `webcam-mic`.
4. Write `<dataset>/<session>/<source>/{raw,clips,...}` and append to `<dataset>/manifest.jsonl`.

The dataset is permanent: this tool never deletes or overwrites anything (existing files are kept,
a re-run adds a revision), directories are mode 700 and files 600.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kivi_real_common as C  # noqa: E402

RATE = C.RATE


# ---------------------------------------------------------------------------------------------
# where the wake files come from
# ---------------------------------------------------------------------------------------------


class LocalFetcher:
    """A directory laid out like /voice-recordings (tests and offline copies)."""

    def __init__(self, root):
        self.root = Path(root)

    def list_wake_files(self, prefix):
        out = []
        for p in sorted(glob.glob(str(self.root / f"{prefix}*" / "*" / "*" / "00_wake*.wav"))):
            st = os.stat(p)
            out.append({"path": p, "size": st.st_size, "mtime": st.st_mtime})
        return out

    def fetch(self, path):
        with open(path, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    return
                yield b


class KubectlFetcher:
    """The HA pod, read only: one `sh -c` with a glob and `stat`, and `cat` per file."""

    def __init__(self, context, namespace, pod, container=None, root="/voice-recordings"):
        self.base = ["kubectl", "--context", context, "-n", namespace, "exec", pod]
        if container:
            self.base += ["-c", container]
        self.root = root

    def list_wake_files(self, prefix):
        assert re.fullmatch(r"[0-9a-f]{4,32}", prefix)
        script = (f'for f in {self.root}/{prefix}*/*/*/00_wake*.wav; do '
                  '[ -f "$f" ] && echo "$f $(stat -c "%s %Y" "$f")"; done')
        r = subprocess.run(self.base + ["--", "sh", "-c", script], capture_output=True, text=True, timeout=300)
        if r.returncode not in (0, 1):
            raise SystemExit(f"kubectl exec failed ({r.returncode}): {r.stderr.strip()[:300]}")
        out = []
        for line in r.stdout.splitlines():
            m = re.fullmatch(r"(\S+) (\d+) (\d+)", line.strip())
            if m:
                out.append({"path": m.group(1), "size": int(m.group(2)), "mtime": float(m.group(3))})
        return out

    def fetch(self, path):
        p = subprocess.Popen(self.base + ["--", "cat", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        while True:
            b = p.stdout.read(1 << 20)
            if not b:
                break
            yield b
        if p.wait() != 0:
            raise SystemExit(f"kubectl cat {path} failed: {p.stderr.read().decode()[:300]}")


def run_of(path):
    parts = Path(path).parts
    return {"pipeline": parts[-3], "run": parts[-2]}


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

    def timeline(self):
        """Samples on the reference clock (zeros where this source has no audio), with one
        offset per file (its value at the middle of the overlap) and the covered intervals."""
        tl = np.zeros(self.ref_len, np.int16)
        cov = []
        for f in self.files:
            dur_ref, sig_s = self.ref_len / RATE, len(f["pcm"]) / RATE
            lo, hi = max(0.0, -f["a"]), min(dur_ref, sig_s - f["a"])
            if hi <= lo:
                continue
            D = int(round(self.d(f, (lo + hi) / 2) * RATE))
            i0, i1 = max(0, -D), min(self.ref_len, len(f["pcm"]) - D)
            if i1 > i0:
                tl[i0:i1] = f["pcm"][i0 + D:i1 + D]
                cov.append([round(i0 / RATE, 3), round(i1 / RATE, 3)])
        return tl, cov


def fetch_raw(fetcher, f, rawdir):
    """Copy one wake file into `rawdir` as a proper WAV (PCM from offset 44). Reuses a local copy of
    the same size; a file that grew since gets a new name. Returns (path, name)."""
    name = f"{f['run']}.wav"
    dst = rawdir / name
    if dst.exists() and dst.stat().st_size != f["size"]:
        name = f"{f['run']}-{f['size']}.wav"
        dst = rawdir / name
    if not dst.exists():
        w, skip = C.WavWriter(dst), 44
        try:
            for chunk in fetcher.fetch(f["path"]):
                if skip:
                    cut = min(skip, len(chunk))
                    chunk, skip = chunk[cut:], skip - cut
                w.write(chunk)
        finally:
            w.close()
    return dst, name


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
    so every press is one row of `states`."""

    def __init__(self, context, namespace, pod, container, user, db, entity="input_button.kivi_take"):
        self.base = ["kubectl", "--context", context, "-n", namespace, "exec", pod]
        if container:
            self.base += ["-c", container]
        self.user, self.db, self.entity = user, db, entity
        if not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", entity):
            raise SystemExit(f"bad entity id {entity!r}")

    def presses(self, since, until):
        sql = ("SELECT s.last_updated_ts FROM states s JOIN states_meta m ON s.metadata_id = m.metadata_id "
               f"WHERE m.entity_id = '{self.entity}' AND s.state ~ '^20[0-9][0-9]-' "
               f"AND s.last_updated_ts BETWEEN {float(since)} AND {float(until)} ORDER BY 1")
        assert sql.lstrip().upper().startswith("SELECT") and ";" not in sql
        cmd = self.base + ["--", "env", "PGOPTIONS=-c default_transaction_read_only=on",
                           "psql", "-U", self.user, "-d", self.db, "-At", "-c", sql]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise SystemExit(f"reading the button presses failed: {r.stderr.strip()[:300]}\n"
                             "Use --presses-file with a JSON list of ISO times instead.")
        return sorted(float(x) for x in r.stdout.split())


def parse_utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def utc_str(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def copy_new(src, dst):
    """Copy a file into the dataset; never overwrites. Returns False when `dst` already exists."""
    if Path(dst).exists():
        return False
    with open(src, "rb") as fi, C.create_exclusive(dst) as fo:
        while True:
            b = fi.read(1 << 20)
            if not b:
                break
            fo.write(b)
    return True


def summarize_alignment(label, run, al):
    return {"source": label, "run": run, "ok": al["ok"], "offset_a_s": round(al["a"], 4),
            "prior_s": round(al["d_prior_s"], 4), "drift_ppm": al["drift_ppm"],
            "drift_over_session_s": al["drift_over_session_s"], "windows": al["n_windows"],
            "confident": al["n_confident"], "inliers": al["n_inliers"],
            "median_conf": round(al["median_conf"], 1), "resid_std_ms": al["resid_std_ms"]}


# ---------------------------------------------------------------------------------------------
# extraction
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
    exid = "ex-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    rev = a.revision or ""
    kw = dict(coarse_lag_s=a.coarse_lag_s, refine_lag_s=a.refine_lag_s, win_s=a.win_s, min_conf=a.min_conf)

    # which wake files
    tv_files = []
    if not a.no_tv:
        prefix = C.device_prefix(device, a.devices_file, a.device_id_prefix)
        fetcher = fetcher or KubectlFetcher(a.context, a.namespace, a.pod, a.container, a.recordings_root)
        for f in fetcher.list_wake_files(prefix):
            dur = max(0, f["size"] - 44) / 2 / RATE
            f.update(dur=dur, start=f["mtime"] - dur, **run_of(f["path"]))
            if f["mtime"] >= t0 - a.slack and f["start"] <= t_end + a.slack:
                tv_files.append(f)
        tv_files.sort(key=lambda f: f["start"])
        print(f"{tv}: {len(tv_files)} wake file(s) overlap the session window (device {device}, "
              f"id prefix {prefix}); session {ref_dur / 60:.1f} min")
        if not tv_files:
            print("WARNING: no wake file overlaps the session. Was the satellite unmuted and connected? "
                  "Continuing with the PC sources only.")
    if a.dry_run:
        for f in tv_files:
            print(f"  would copy {f['pipeline']}/{f['run']}: {f['dur']:.0f} s, ends {datetime.fromtimestamp(f['mtime'], timezone.utc):%H:%M:%SZ}")
        print(f"  would cut {len(C.cue_windows(session, a.pad_before, a.pad_after))} cue clips and "
              f"the non-cue segments per source into {a.dataset_root}/{sid}")
        return None

    root = Path(a.dataset_root).expanduser()
    C.private_mkdir(root, root)
    sroot = C.private_mkdir(root / sid, root)
    manifest = root / "manifest.jsonl"
    summaries, report_streams = [], {}

    # reference + webcam
    for label, src in (("pc-mic", "pc-mic.wav"), ("webcam-mic", "webcam-mic.wav")):
        raw = C.private_mkdir(sroot / label / "raw", root)
        copy_new(sdir / src, raw / src)
    streams = {"pc-mic": Stream("pc-mic", len(pc))}
    streams["pc-mic"].add(pc, 0.0, 0.0, run="", name="pc-mic.wav", ok=True, conf=None, drift_ppm=0.0)
    al = C.align_stream(pc, web, o_pc - o_web, coarse_lag_s=2.0, refine_lag_s=0.5, win_s=a.win_s, min_conf=a.min_conf)
    summaries.append(summarize_alignment("webcam-mic", "", al))
    streams["webcam-mic"] = Stream("webcam-mic", len(pc))
    streams["webcam-mic"].add(web, al["a"], al["b"], run="", name="webcam-mic.wav", ok=al["ok"],
                              conf=al["median_conf"], drift_ppm=al["drift_ppm"], align=al)

    # satellite
    if tv_files:
        streams[tv] = Stream(tv, len(pc))
        raw = C.private_mkdir(sroot / tv / "raw", root)
        for f in tv_files:
            dst, name = fetch_raw(fetcher, f, raw)
            sig = C.read_pcm16(dst)
            dur = len(sig) / RATE
            s_est = (f["mtime"] - dur) - t0          # session time of the file's first sample
            al = C.align_stream(pc, sig, o_pc - s_est, **kw)
            summaries.append(summarize_alignment(tv, f["run"], al))
            streams[tv].add(sig, al["a"], al["b"], run=f"{f['pipeline']}/{f['run']}", name=name, ok=al["ok"],
                            conf=al["median_conf"], drift_ppm=al["drift_ppm"], align=al)

    # timelines and alignment records
    for label, st in streams.items():
        if label == "pc-mic":
            cov, tlname = [[0.0, round(ref_dur, 3)]], "raw/pc-mic.wav"
        else:
            tl, cov = st.timeline()
            tlname = "timeline.wav"
            if (sroot / label / tlname).exists():
                tlname = f"timeline-{exid}.wav"
            C.write_wav_new(sroot / label / tlname, tl)
        aname = "alignment.json" if not (sroot / label / "alignment.json").exists() else f"alignment-{exid}.json"
        files = [{"name": f["name"], "run": f["run"], "ok": bool(f["ok"]), "offset_a_s": round(f["a"], 5),
                  "drift_ppm": f["drift_ppm"], "median_conf": f["conf"],
                  "windows": [] if "align" not in f else f["align"]["windows"]} for f in st.files]
        C.write_json_new(sroot / label / aname, {"extract_id": exid, "source": label, "reference": "pc-mic",
                                                 "timeline": tlname, "ref_samples": len(pc), "coverage_s": cov,
                                                 "pc_offset_s": o_pc, "files": files})
        report_streams[label] = {"timeline": tlname, "alignment": aname, "coverage_s": cov}

    # clips
    windows = C.cue_windows(session, a.pad_before, a.pad_after)
    segs = C.noncue_segments(session, windows)
    blocks = {b["index"]: b for b in session["blocks"]}
    done = {(r["source"], r["clip"]) for r in C.read_jsonl(manifest) if r["session"] == sid}
    rows, skipped = [], {}
    cdir = "clips" + (f"-{C.ascii_slug(rev)}" if rev else "")
    items = [("cue", w["block"], w["cue"], w["label"], w["word"], w["t0"], w["t1"], w["cue_offset_s"]) for w in windows]
    for n, (bi, lab, t0_, t1_) in enumerate(segs, 1):
        items.append(("seg", bi, n, lab, "", t0_, t1_, None))
    for label, st in streams.items():
        for kind, bi, idx, lab, word, t0_, t1_, cue_off in items:
            b = blocks[bi]
            got = st.cut(t0_ - o_pc, t1_ - o_pc)
            if got is None:
                skipped[label] = skipped.get(label, 0) + 1
                continue
            pcm, d_used, f = got
            stem = f"{'c' if kind == 'cue' else 'g'}{idx:03d}-{C.ascii_slug(lab)}"
            rel = f"{sid}/{label}/{cdir}/{bi:02d}-{C.ascii_slug(b['name'])}/{stem}.wav"
            if (label, rel) in done:
                continue
            C.private_mkdir(root / Path(rel).parent, root)
            if (root / rel).exists():
                continue
            C.write_wav_new(root / rel, pcm)
            rows.append(C.make_row(
                origin="session", session=sid, source=label, device=device if label == tv else label,
                session_device=device, speaker=session.get("speaker", ""),
                split=C.channel_split(session.get("split", "unassigned"), label), eval_channel=C.is_eval_channel(label),
                condition=C.condition_string(b), conditions=b.get("conditions", {}), tags=session.get("tags", {}),
                block=bi, block_name=b["name"], label=lab, word=word, kind="cue" if kind == "cue" else "segment",
                clip=rel, start_s=round(t0_, 3), end_s=round(t1_, 3),
                start_utc=utc_str(t0_ + t0), end_utc=utc_str(t1_ + t0),
                cue_index=idx if kind == "cue" else None, cue_offset_s=cue_off,
                align_offset_s=round(d_used, 5), align_conf=f["conf"], align_ok=bool(f["ok"]),
                drift_ppm=f["drift_ppm"], source_run=f["run"], revision=rev, extract_id=exid,
                dry_run_session=bool(session.get("dry_run"))))
    C.append_jsonl(manifest, rows)
    if not (sroot / "session.json").exists():
        C.write_json_new(sroot / "session.json", session)
    report = {"extract_id": exid, "session": sid, "device": device, "revision": rev, "alignment": summaries,
              "clips_written": len(rows), "clips_skipped_uncovered": skipped, "streams": report_streams}
    C.write_json_new(sroot / f"extract-{exid}.json", report)

    print(f"\nWrote {len(rows)} clips to {sroot} and appended them to {manifest}")
    for s in summaries:
        flag = "OK " if s["ok"] else "LOW CONFIDENCE"
        print(f"  align {s['source']:11s} {flag}: offset {s['offset_a_s']:+.3f} s (prior {s['prior_s']:+.3f}), "
              f"{s['confident']}/{s['windows']} windows confident, median PSR {s['median_conf']}, "
              f"drift {s['drift_ppm']:+.1f} ppm = {s['drift_over_session_s'] * 1000:+.0f} ms over the session, "
              f"residual {s['resid_std_ms']} ms")
    if any(not s["ok"] for s in summaries):
        print("  WARNING: a source has low alignment confidence; its clips use the mtime estimate and "
              "may be shifted. Check the TV was audible to the desk mic (TV on, you speaking) or raise "
              "--coarse-lag-s.")
    if skipped:
        print(f"  clips not cut because the source did not cover them: {skipped}")
    return report


def button_takes(a, fetcher=None, presses=None):
    """Takes marked by presses of `input_button.kivi_take`: for each press, find the word in the
    wake audio of the device within 0-6 s after it (energy only), and cut a clip around it."""
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
    fetcher = fetcher or KubectlFetcher(a.context, a.namespace, a.pod, a.container, a.recordings_root)
    files = []
    for f in fetcher.list_wake_files(prefix):
        dur = max(0, f["size"] - 44) / 2 / RATE
        f.update(dur=dur, start=f["mtime"] - dur, **run_of(f["path"]))
        if f["mtime"] >= since - a.slack and f["start"] <= until + a.slack:
            files.append(f)
    files.sort(key=lambda f: f["start"])
    print(f"{len(times)} button press(es) between {a.since} and {a.until}; {len(files)} {tv} wake file(s) overlap")
    if a.dry_run:
        return None
    root = Path(a.dataset_root).expanduser()
    sid = f"button-{datetime.fromtimestamp(since, timezone.utc):%Y%m%dT%H%M%SZ}-{a.speaker}-{tv}"
    sroot = C.private_mkdir(root / sid, root)
    raw = C.private_mkdir(sroot / tv / "raw", root)
    loaded = []
    for f in files:
        dst, name = fetch_raw(fetcher, f, raw)
        loaded.append((f, C.read_pcm16(dst)))
    exid = "ex-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    manifest = root / "manifest.jsonl"
    done = {r["clip"] for r in C.read_jsonl(manifest) if r["session"] == sid}
    rows, stats = [], {"presses": len(times), "no_file": 0, "no_clear_onset": 0, "clips": 0, "already_there": 0}
    after = []
    tol = a.tolerance_s
    for n, t in enumerate(times, 1):
        hit = next(((f, x) for f, x in loaded if f["start"] - tol <= t <= f["mtime"] + tol), None)
        if hit is None:
            stats["no_file"] += 1
            continue
        f, x = hit
        dur = len(x) / RATE
        rel = t - f["start"]
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
        if clip in done or (root / clip).exists():
            stats["already_there"] += 1
            continue
        C.private_mkdir(root / Path(clip).parent, root)
        C.write_wav_new(root / clip, x[int(k0 * RATE):int(k1 * RATE)])
        after.append(w["t0"] - rel)
        stats["clips"] += 1
        rows.append(C.make_row(
            origin="button", session=sid, source=tv, device=device, session_device=device, speaker=a.speaker,
            split=a.split, eval_channel=True, condition="button-take", conditions={}, tags={}, block=1,
            block_name="button-takes", label="kivi", word="kivi", kind="cue", clip=clip,
            start_s=round(f["start"] + k0 - since, 3), end_s=round(f["start"] + k1 - since, 3),
            start_utc=utc_str(f["start"] + k0), end_utc=utc_str(f["start"] + k1), cue_index=n,
            source_run=f"{f['pipeline']}/{f['run']}", revision="", extract_id=exid,
            press_utc=utc_str(t), onset_after_press_s=round(w["t0"] - rel, 2), snr_db=w["snr_db"],
            n_candidates=len(cands)))
    C.append_jsonl(manifest, rows)
    meta = sroot / "session.json"
    if not meta.exists():
        C.write_json_new(meta, {"schema": 1, "session_id": sid, "kind": "button-takes", "device": device,
                                "tv_source": tv, "speaker": a.speaker, "split": a.split, "since": utc_str(since),
                                "until": utc_str(until), "t0_utc": utc_str(since), "blocks": []})
    C.write_json_new(sroot / f"extract-{exid}.json", {"extract_id": exid, **stats,
                     "median_onset_after_press_s": round(float(np.median(after)), 2) if after else None})
    print(f"clips cut: {stats['clips']} of {stats['presses']} presses; no wake file for {stats['no_file']}; "
          f"no clear speech onset for {stats['no_clear_onset']}; already in the dataset {stats['already_there']}"
          + (f"; median onset {np.median(after):.1f} s after the press" if after else ""))
    if stats["no_clear_onset"]:
        print("  (a press without a clear onset means no burst of speech 10 dB above the room within the window; "
              "check the satellite was unmuted and the take was spoken within 6 s of the press)")
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--session-dir", help="a kivi_session.py directory (not with --button-takes)")
    ap.add_argument("--button-takes", action="store_true",
                    help="cut takes marked by input_button.kivi_take presses: needs --device --speaker --since --until")
    ap.add_argument("--device", default="c3-b", help="(--button-takes) the satellite the takes were spoken to")
    ap.add_argument("--speaker", help="(--button-takes) pseudonymous id, e.g. spk02")
    ap.add_argument("--split", default="unassigned", choices=C.SPLITS, help="(--button-takes)")
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
    ap.add_argument("--slack", type=float, default=30.0, help="seconds around the session to accept wake files")
    ap.add_argument("--pad-before", type=float, default=0.5)
    ap.add_argument("--pad-after", type=float, default=3.5)
    ap.add_argument("--coarse-lag-s", type=float, default=20.0)
    ap.add_argument("--refine-lag-s", type=float, default=1.5)
    ap.add_argument("--win-s", type=float, default=15.0)
    ap.add_argument("--min-conf", type=float, default=8.0, help="minimum PSR of a trusted alignment window")
    ap.add_argument("--revision", help="tag for a re-extraction with other settings (clips-<tag>/)")
    ap.add_argument("--no-tv", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    fetcher = LocalFetcher(a.from_dir) if a.from_dir else None
    if a.button_takes:
        if not (a.speaker and a.since and a.until):
            raise SystemExit("--button-takes needs --speaker, --since and --until")
        button_takes(a, fetcher)
    else:
        if not a.session_dir:
            raise SystemExit("--session-dir is required (or use --button-takes)")
        extract(a, fetcher)


if __name__ == "__main__":
    main()
