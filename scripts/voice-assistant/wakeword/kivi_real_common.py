"""Shared helpers of the real-voice tools (kivi_session.py, kivi_extract.py, kivi_score.py).

Nothing here opens a microphone, plays audio or deletes anything. Contents: WAV reading and writing
(the Home Assistant wake files carry a placeholder header while they are open, so PCM is always read
from the data chunk to the end of the file), the device and speaker naming rules, the cue-window
plan shared by the extractor and the scorer, GCC-PHAT alignment of two recordings, and the private
(mode 700/600, never overwritten) dataset helpers.
"""
import fcntl
import json
import os
import re
import struct
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import numpy as np

RATE = 16000
HERE = Path(__file__).resolve().parent
DEFAULT_DEVICES_FILE = HERE / "kivi_devices.json"
LEGACY_HOME = "/work/workspaces/kivi-wakeword"      # before 2026-11-12; after the move it is a symlink to ~/kivi-models/data


def kivi_home():
    """The workspace of the wake word work (datasets, releases, scores, venv ...): `$KIVI_HOME`, else `$WORK` (the older
    variable), else `~/kivi-models/data` (gitignored, inside the kivi-models checkout), else the legacy path."""
    for c in (os.environ.get("KIVI_HOME"), os.environ.get("WORK"), "~/kivi-models/data", LEGACY_HOME):
        if c and os.path.isdir(os.path.expanduser(c)):
            return Path(os.path.expanduser(c))
    return Path(LEGACY_HOME)


def kivi_models_repo():
    """The kivi-models checkout: `$KIVI_MODELS`, else `~/kivi-models`, else `<home>/repo/kivi-models` (legacy)."""
    for c in (os.environ.get("KIVI_MODELS"), "~/kivi-models", str(kivi_home() / "repo/kivi-models")):
        if c and (Path(c).expanduser() / "models.lock").exists():
            return Path(c).expanduser()
    return kivi_home() / "repo/kivi-models"


DEFAULT_DATASET_ROOT = str(kivi_home() / "datasets/real")
DAY_TZ = "Europe/Madrid"


def day_role(local_date):
    """Day split of the real-audio work (owner decision 2026-11-11): by Europe/Madrid local date, the day of the
    month decides: odd = `test` (never used for any choice), even = `mine` (train / mining / selection). `local_date`
    is a `datetime.date`. Parity of the day of the month, not alternation by day count (which breaks at Dec 31 to
    Jan 1, both odd), the same rule as `kivi_live_wakes.propose_split_alternate`."""
    return "test" if local_date.day % 2 else "mine"


def epoch_day_role(t):
    """`day_role` of the Europe/Madrid local date of the epoch time `t`."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return day_role(datetime.fromtimestamp(t, ZoneInfo(DAY_TZ)).date())


def split_at_local_midnight(intervals, tz=DAY_TZ):
    """Cut each (t0, t1) epoch interval at every local midnight of `tz`."""
    from datetime import datetime, time as dtime, timedelta
    from zoneinfo import ZoneInfo
    z = ZoneInfo(tz)
    out = []
    for a, b in intervals:
        cur = a
        while cur < b:
            d = datetime.fromtimestamp(cur, z).date() + timedelta(days=1)
            nxt = datetime.combine(d, dtime(0, 0), z).timestamp()
            out.append((cur, min(b, nxt)))
            cur = nxt
    return out
PC_SOURCES = ("pc-mic", "webcam-mic")
SPLITS = ("unassigned", "train", "dev", "test", "train-only")
# Split policy (docs/voice-assistant/experiments/kivi-wake-word.md): the split belongs to the session,
# so every channel of one utterance and every take of a session share it. Hold out by speaker and by
# device. Rule change (owner decision 2026-11-11): on a test-split (odd Europe/Madrid date) session every
# source is an EVALUATION channel, reported per device class (tv, pc-desk, pc-webcam, phone) and never pooled
# with the TV gate; the TV mic stays the gate for the deployed TV path. In a train or unassigned session the
# PC and webcam channels are tagged `train-only` (never evaluation); in a dev session they carry `dev` and are
# not evaluation; in a test session they carry `test` (so they can never leak into training) and are evaluation.
# A phone memo carries the split given at import. `eval_channel` is DERIVED from (source, split) by
# `is_eval_channel`, also for manifest rows written before the rule change (their stored field says False).

# ---------------------------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------------------------


def valid_device(name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", name or ""):
        raise SystemExit(f"bad device name {name!r}: lowercase letters, digits and '-' only (e.g. c3-a)")
    return name


def source_label(device):
    """`c3-a` -> `tv-c3a`: the label of the satellite's wake-stage audio in the dataset."""
    return "tv-" + valid_device(device).replace("-", "")


def valid_speaker(spk):
    """Speakers are pseudonymous ids (spk01, spk02, ...). Names are refused on purpose."""
    if not re.fullmatch(r"spk\d{2,3}", spk or ""):
        raise SystemExit(f"bad speaker {spk!r}: use a pseudonymous id like spk01 (never a name; "
                         "the mapping stays with the owner)")
    return spk


def load_devices(path=None):
    p = Path(path) if path else DEFAULT_DEVICES_FILE
    d = json.loads(p.read_text(encoding="utf-8"))
    return {k: v for k, v in d.items() if not k.startswith("_")}


def device_prefix(device, devices_file=None, override=None):
    """HA device-id prefix of a satellite from the small config (or an explicit override)."""
    valid_device(device)
    if override:
        prefix = override
    else:
        devs = load_devices(devices_file)
        if device not in devs:
            raise SystemExit(f"device {device!r} is not in {devices_file or DEFAULT_DEVICES_FILE}; "
                             "add it there or pass --device-id-prefix")
        prefix = devs[device]["device_id_prefix"]
    if not re.fullmatch(r"[0-9a-f]{4,32}", prefix):
        raise SystemExit(f"bad device id prefix {prefix!r} (hex digits only)")
    return prefix


def ascii_slug(text):
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9.]+", "-", t).strip("-").lower() or "x"


# ---------------------------------------------------------------------------------------------
# WAV
# ---------------------------------------------------------------------------------------------


def wav_header(n_bytes, rate=RATE):
    if n_bytes > 0xFFFFFFFF - 36:
        raise OverflowError("a WAV data chunk is limited to 4 GiB: copy a smaller range")
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + n_bytes, b"WAVE", b"fmt ", 16, 1, 1, rate,
                       rate * 2, 2, 16, b"data", n_bytes)


def create_exclusive(path, mode=0o600):
    """Open a new file; never overwrites (O_EXCL)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    return os.fdopen(fd, "wb")


def part_path(path):
    """A unique sibling name for a file being written: `<name>.<pid>-<ns>.part`."""
    return Path(f"{path}.{os.getpid()}-{time.time_ns()}.part")


def finalize_part(part, final):
    """Move a finished `.part` file to its final name. The only rename in the dataset tools; it
    refuses to replace an existing file, so nothing in the dataset is ever overwritten."""
    if Path(final).exists():
        raise FileExistsError(str(final))
    os.rename(part, final)


class WavWriter:
    """Incremental 16 kHz mono S16 writer. The header is patched on `flush_header`, so a crash
    leaves a file whose PCM can still be read from offset 44. Used for the owner's live
    recording; the dataset uses `PartWavWriter`."""

    def __init__(self, path, rate=RATE):
        self.f = create_exclusive(path)
        self.rate = rate
        self.n = 0
        self.f.write(wav_header(0, rate))

    def write(self, pcm_bytes):
        self.f.write(pcm_bytes)
        self.n += len(pcm_bytes)

    def flush_header(self):
        self.f.flush()
        self.f.seek(0)
        self.f.write(wav_header(self.n, self.rate))
        self.f.seek(0, os.SEEK_END)
        self.f.flush()

    def close(self):
        if not self.f.closed:
            self.flush_header()
            self.f.close()


class PartWavWriter(WavWriter):
    """Dataset WAV: written as `<name>.<nonce>.part`, fsynced, and renamed only by `commit` after
    the byte count was checked. If the copy fails the `.part` stays and the final name never appears."""

    def __init__(self, path, rate=RATE):
        self.final = Path(path)
        if self.final.exists():
            raise FileExistsError(str(path))
        self.part = part_path(path)
        super().__init__(self.part, rate)

    def commit(self, expected_bytes=None):
        if expected_bytes is not None and self.n != expected_bytes:
            self.close()
            raise OSError(f"short or long copy of {self.final.name}: got {self.n} bytes, expected {expected_bytes}")
        self.close_sync()
        finalize_part(self.part, self.final)

    def close_sync(self):
        if not self.f.closed:
            self.flush_header()
            os.fsync(self.f.fileno())
            self.f.close()


def write_wav_new(path, pcm16):
    w = PartWavWriter(path)
    w.write(np.ascontiguousarray(pcm16, dtype="<i2").tobytes())
    w.commit()


def write_bytes_new(path, data):
    part = part_path(path)
    with create_exclusive(part) as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    finalize_part(part, path)


def read_pcm16(path):
    """int16 PCM of a 16 kHz mono S16 WAV, from its data chunk to the end of the file (the size
    fields are ignored: a still-open Home Assistant file has placeholders)."""
    raw = Path(path).read_bytes()
    off = 44
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        p = 12
        while p + 8 <= len(raw):
            tag, size = struct.unpack_from("<4sI", raw, p)
            if tag == b"fmt ":
                fmt, ch, rate, _, _, bits = struct.unpack_from("<HHIIHH", raw, p + 8)
                if (fmt, ch, rate, bits) != (1, 1, RATE, 16):
                    raise SystemExit(f"{path}: need 16 kHz mono S16, got fmt={fmt} ch={ch} "
                                     f"rate={rate} bits={bits}")
            elif tag == b"data":
                off = p + 8
                break
            p += 8 + size + (size & 1)
    data = raw[off:]
    return np.frombuffer(data[:len(data) // 2 * 2], dtype="<i2").copy()


# ---------------------------------------------------------------------------------------------
# private dataset helpers: modes 700/600, no overwrite, no deletion
# ---------------------------------------------------------------------------------------------


def private_mkdir(path, root):
    """Create `path` (and parents below `root`) with mode 700. `root` itself is made 700 too."""
    root, path = Path(root), Path(path)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)  # only `root` itself is forced to 700
    for d in [root] + [p for p in reversed(path.parents) if root in p.parents] + [path]:
        if d == root or root in d.parents:
            if not d.exists():
                d.mkdir(mode=0o700)
            os.chmod(d, 0o700)
    return path


def append_jsonl(path, rows):
    """Append rows one line at a time: each line is one `write` on an O_APPEND descriptor followed
    by `fsync`, so a crash leaves whole lines plus at most one truncated last line. The file is mode 600."""
    repair_jsonl(path)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        for r in rows:
            os.write(fd, (json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(fd)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def repair_jsonl(path):
    """Terminate a truncated last line (a crash mid-write) with a newline, without deleting anything;
    `read_jsonl` skips such a line with a warning."""
    p = Path(path)
    if p.exists() and p.stat().st_size:
        with open(p, "rb") as f:
            f.seek(-1, os.SEEK_END)
            last = f.read(1)
        if last != b"\n":
            print(f"WARNING: {p.name} ended in a truncated line; terminated it (the line is skipped)", file=sys.stderr)
            fd = os.open(str(p), os.O_WRONLY | os.O_APPEND)
            os.write(fd, b"\n")
            os.fsync(fd)
            os.close(fd)


def read_jsonl(path):
    """All valid rows; a line that is not valid JSON (a truncated write) is skipped with a warning."""
    if not Path(path).exists():
        return []
    rows = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            print(f"WARNING: {Path(path).name} line {n} is not valid JSON; skipped", file=sys.stderr)
    return rows


def write_json_new(path, obj):
    write_bytes_new(path, (json.dumps(obj, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))


DEFAULT_PHRASE = "kivi"        # the wake phrase of every row recorded before the `phrase` field existed


def normalize_phrase(text):
    """A wake phrase as stored: lower case, single spaces, letters only (Spanish letters allowed)."""
    p = " ".join(str(text).lower().split())
    if not re.fullmatch(r"[a-z\u00e1\u00e9\u00ed\u00f3\u00fa\u00fc\u00f1]+( [a-z\u00e1\u00e9\u00ed\u00f3\u00fa\u00fc\u00f1]+){0,3}", p):
        raise SystemExit(f"bad phrase {text!r}: one to four words of letters")
    return p


def row_phrase(r):
    """The wake phrase a manifest row was recorded for. Rows without the field (recorded before it existed)
    are `kivi`. Room-audio negatives (kind `negative`) belong to no phrase: None."""
    if r.get("kind") == "negative":
        return None
    return r.get("phrase") or DEFAULT_PHRASE


def training_rows(rows, exclude_speakers=(), include_unassigned=False, phrase=None):
    """Manifest rows that may train the model: split train or train-only, never `needs_review`, never
    `excluded` or `superseded` (rows read with `read_manifest`), and none from `exclude_speakers` (the unseen-speaker
    ablation excludes ALL data of that speaker). Pass rows from `read_manifest`, so that later split
    decisions and superseded marks are applied.

    `phrase` keeps only rows recorded for that wake phrase (old rows count as `kivi`) plus phrase-free
    rows (room-audio negatives). Rows of other phrases are not lost: `negative_rows_for_phrase` returns them."""
    ok = {"train", "train-only"} | ({"unassigned"} if include_unassigned else set())
    out = [r for r in rows if r.get("split") in ok and not r.get("needs_review") and not r.get("superseded")
           and not r.get("excluded") and r.get("speaker") not in set(exclude_speakers)]
    if phrase is not None:
        want = normalize_phrase(phrase)
        out = [r for r in out if row_phrase(r) in (None, want)]
    return out


def negative_rows_for_phrase(rows, phrase, **kw):
    """Rows whose spoken content is NOT `phrase`, usable as negatives for it (same filters as
    `training_rows`: split, needs_review, superseded, `exclude_speakers=`, `include_unassigned=`): the old
    "kivi" takes become negatives for "oye kivi" (and the reverse), together with the background segments
    and the near-word takes of every session — including a session recorded FOR `phrase`, whose `near:*`
    cues are exactly the confusers to push down. A row whose label or word is the target phrase itself
    (that session's positive takes) is left out. Each row is a copy with `negative_for` set and its own
    label kept in `label_original`; the audio is the same file."""
    want = normalize_phrase(phrase)
    out = []
    for r in training_rows(rows, **kw):
        if row_phrase(r) is None:
            continue
        label = str(r.get("label", "")).lower()
        if want in (label, label.removeprefix("near:"), str(r.get("word", "")).lower()):
            continue
        out.append({**r, "negative_for": want, "label_original": r.get("label")})
    return out


# The manifest is append-only and the tools never rewrite it. Later decisions live in two append-only
# files next to it, applied by `read_manifest`:
#   splits.jsonl      {session, split, reason, set_utc}: the owner's split for a whole session (the last record wins)
#   superseded.jsonl  {session, revision, source (optional), superseded_by, reason, set_utc}: rows of that
#                     extraction revision are wrong (e.g. a bad time mapping) and must never be used; the audio stays.
def read_marks(root):
    root = Path(root)
    splits = {r["session"]: r["split"] for r in read_jsonl(root / "splits.jsonl")}
    return splits, read_jsonl(root / "superseded.jsonl")


def read_excluded(root):
    """excluded.jsonl records: {session, revision, source (optional), block (optional), cue_index [..] (optional),
    reason, set_utc}. A record with `block` covers the cue rows of that block name; one with `cue_index` covers
    those indices; both together must match both. A row with no `cue_index` (a segment) is never excluded."""
    return read_jsonl(Path(root) / "excluded.jsonl")


def excluded_match(m, r):
    """Whether an excluded.jsonl record `m` covers manifest row `r`. Segments and room negatives have no
    `cue_index` and are never covered; `block` is matched against the row's `block_name`, and a record with
    no `cue_index` list covers every cue of the block."""
    if r.get("kind") != "cue" or m["session"] != r.get("session"):
        return False
    idx = m.get("cue_index") or []
    return ((m.get("revision") or "") == (r.get("revision") or "") and m.get("source") in (None, r.get("source"))
            and m.get("block") in (None, r.get("block_name")) and (not idx or r.get("cue_index") in idx))


def read_manifest(root):
    """The manifest rows with the marks applied: `split` becomes the split decided later for session
    rows (through `channel_split`, the original is kept in `split_in_manifest`), and `superseded` is
    True for rows that a superseded mark covers; `phrase` is filled in (old rows are "kivi")."""
    root = Path(root)
    splits, sup = read_marks(root)
    exc = read_excluded(root)
    out = []
    for r in read_jsonl(root / "manifest.jsonl"):
        r = dict(r)
        r["phrase"] = row_phrase(r)               # old rows (no field) are "kivi"; room negatives have none
        if r.get("origin") in ("session", "button") and r.get("session") in splits:
            r["split_in_manifest"] = r.get("split")
            r["split"] = channel_split(splits[r["session"]], r.get("source"))
        r["superseded"] = any(m["session"] == r.get("session") and (m.get("revision") or "") == (r.get("revision") or "")
                              and m.get("source") in (None, r.get("source")) for m in sup)
        r["excluded"] = any(excluded_match(m, r) for m in exc)
        out.append(r)
    return out


def session_split(root, session):
    """The split of a session: the last decision in splits.jsonl, else the one in its session.json."""
    d = read_marks(root)[0]
    if session["session_id"] in d:
        return d[session["session_id"]]
    return session.get("split", "")


def sample_drift(samples, elapsed_s, rate=RATE):
    """Relative difference between the recorded audio length and the elapsed time."""
    return (samples / rate - elapsed_s) / elapsed_s if elapsed_s > 0 else 0.0


def pipeline_preflight(context, namespace, pod, container=None, model="kivi", text=None,
                       path="/config/.storage/assist_pipeline.pipelines"):
    """Wake word ids of the Assist pipelines, read-only (`kubectl exec ... cat`). `live` is True when
    the preferred pipeline (or any pipeline, listed in `others`) uses `model` as its wake word: a
    detection then closes the satellite's wake file mid-session. The device is assumed to use the
    preferred pipeline unless its own selection says otherwise (not read here)."""
    try:
        if text is None:
            cmd = ["kubectl", "--context", context, "-n", namespace, "exec", pod] + (["-c", container] if container else []) + ["--", "cat", path]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                return {"error": r.stderr.strip()[:200]}
            text = r.stdout
        data = json.loads(text)["data"]
        items = data["items"]
        pref = next((i for i in items if i.get("id") == data.get("preferred_item")), None)
        wake = (pref or {}).get("wake_word_id")
        others = sorted({i.get("wake_word_id") for i in items if i.get("wake_word_id")} - {wake})
        return {"preferred_wake_word": wake, "others": others,
                "live": bool(wake and model in wake), "any_live": any(model in (o or "") for o in others)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------------------------
# protocol windows: shared by the extractor (clips) and the scorer (cue windows, non-cue time)
# ---------------------------------------------------------------------------------------------


MANIFEST_FIELDS = (
    "origin", "session", "source", "device", "session_device", "speaker", "split", "eval_channel", "condition",
    "conditions", "tags", "block", "block_name", "label", "word", "kind", "clip", "start_s", "end_s",
    "start_utc", "end_utc", "cue_index", "cue_offset_s", "align_offset_s", "align_conf", "align_ok",
    "drift_ppm", "source_run", "revision", "extract_id", "dry_run_session", "press_utc",
    "onset_after_press_s", "snr_db", "n_candidates", "consent_confirmed", "needs_review", "split_requested", "run_end_in_window",
    "stage", "phrase", "device_class",         # "wake" or "stt": which Assist stage the satellite audio was recorded at (None for desk sources)
    "src_sample0")                            # room pieces: sample of the source wake file where the piece starts (None before 2026-11-12);
                                              # the server's 1280-sample grid is anchored at sample 0 of the file, so a replay of the piece
                                              # must start on (-src_sample0) % 1280 (Phase 0 audit: other grids flip near-threshold events)


def make_row(**kw):
    """One manifest row with every field present (None when not applicable), so all tools write
    the same schema. Unknown keys are an error."""
    bad = set(kw) - set(MANIFEST_FIELDS)
    if bad:
        raise KeyError(f"unknown manifest fields {sorted(bad)}")
    return {k: kw.get(k) for k in MANIFEST_FIELDS}


def channel_split(session_split, source):
    """Split of one channel of a session under the policy above."""
    if source in PC_SOURCES or source == "phone":
        return "train-only" if session_split in ("unassigned", "train", "train-only") else session_split
    return session_split


DEVICE_CLASSES = ("tv", "pc-desk", "pc-webcam", "phone", "other")


def device_class(source):
    """The class of a dataset source: tv (a satellite, `tv-<name>`), pc-desk (pc-mic), pc-webcam (webcam-mic),
    phone, else other. Derived from `source`, so rows written before the `device_class` field have one."""
    s = source or ""
    if s.startswith("tv-"):
        return "tv"
    return {"pc-mic": "pc-desk", "webcam-mic": "pc-webcam", "phone": "phone"}.get(s, "other")


def is_eval_channel(source, split=None):
    """A satellite (tv) channel is always an evaluation channel (the gate reads its test rows). Any other
    source (PC desk mic, webcam, phone) is one when its split is `test` (owner decision 2026-11-11); with
    no split given it is not."""
    if device_class(source) == "tv":
        return True
    return split == "test"


def row_eval_channel(row):
    """eval_channel of a manifest row: derived from its source and split (the stored field of rows written
    before 2026-11-11 is not trusted for PC and phone sources, which were stored as False)."""
    return is_eval_channel(row.get("source"), row.get("split"))


def energy_segments(pcm, t_offset=0.0, *, frame_s=0.02, margin_db=10.0, min_len_s=0.15, max_len_s=3.0,
                    merge_gap_s=0.25, floor_pct=20.0, hang_s=0.04, floor_db=None):
    """Speech-like bursts by short-time energy (no model, no text): frames more than `margin_db`
    above the `floor_pct` percentile of the frame energies of `pcm` (so pass the audio with some
    surrounding context). Bursts closer than `merge_gap_s` are merged; bursts shorter than
    `min_len_s` dropped; longer than `max_len_s` flagged `long`. Returns dicts
    (t0, t1 in seconds + t_offset, snr_db over the floor, peak_db, long). `floor_db` (dBFS) replaces the
    percentile, e.g. one floor measured on a whole recording instead of a short context."""
    n = int(frame_s * RATE)
    nf = len(pcm) // n
    if nf < 5:
        return []
    x = pcm[:nf * n].astype(np.float32).reshape(nf, n) / 32768.0
    db = 20 * np.log10(np.sqrt((x ** 2).mean(axis=1)) + 1e-7)
    floor = float(np.percentile(db, floor_pct)) if floor_db is None else float(floor_db)
    act = db > floor + margin_db
    segs, i = [], 0
    while i < nf:
        if not act[i]:
            i += 1
            continue
        j = i
        while j < nf and act[j]:
            j += 1
        if segs and (i - segs[-1][1]) * frame_s <= merge_gap_s:
            segs[-1][1] = j
        else:
            segs.append([i, j])
        i = j
    out = []
    hang = int(round(hang_s / frame_s))
    for a, b in segs:
        a, b = max(0, a - hang), min(nf, b + hang)
        dur = (b - a) * frame_s
        if dur < min_len_s:
            continue
        out.append({"t0": t_offset + a * frame_s, "t1": t_offset + b * frame_s,
                    "snr_db": round(float(db[a:b].mean() - floor), 1), "peak_db": round(float(db[a:b].max()), 1),
                    "long": dur > max_len_s})
    return out


def session_pc_offset(session):
    """Session time (s since t0) of the first sample of the pc-mic file: the reference clock."""
    return float(session["sources"]["pc-mic"]["first_sample_offset_s"])


def cue_windows(session, pad_before=0.5, pad_after=3.5):
    """One window per cue, in session seconds: [cue - pad_before, cue + pad_after], shortened so
    that it ends pad_before before the next cue of the same block. Labels: kivi, near:<word>."""
    out = []
    for b in session["blocks"]:
        cues = sorted(b.get("cues", []), key=lambda c: c["offset_s"])
        for i, c in enumerate(cues):
            t0 = c["offset_s"] - pad_before
            t1 = c["offset_s"] + pad_after
            if i + 1 < len(cues):
                t1 = min(t1, cues[i + 1]["offset_s"] - pad_before)
            out.append({"block": b["index"], "cue": c["index"], "label": c["label"], "word": c.get("word", ""),
                        "t0": t0, "t1": t1, "cue_offset_s": c["offset_s"]})
    return out


def noncue_segments(session, windows, min_len=1.0, max_len=30.0):
    """Block time outside the cue windows, in session seconds, as (block, label, t0, t1).
    Silence blocks are one segment labelled `silence`; others are `background`. Segments shorter
    than `min_len` are dropped, longer ones are cut into pieces of at most `max_len` (silence
    blocks are kept whole). Pauses between blocks are not part of any block and never appear."""
    segs = []
    for b in session["blocks"]:
        b0, b1 = b["start_offset_s"], b.get("end_offset_s")
        if b1 is None or b1 <= b0:
            continue
        if b["kind"] == "silence":
            segs.append((b["index"], "silence", b0, b1))
            continue
        ws = sorted((w["t0"], w["t1"]) for w in windows if w["block"] == b["index"])
        cur = b0
        gaps = []
        for w0, w1 in ws:
            if w0 > cur:
                gaps.append((cur, w0))
            cur = max(cur, w1)
        if cur < b1:
            gaps.append((cur, b1))
        for g0, g1 in gaps:
            t = g0
            while g1 - t > max(min_len, 1e-6):
                e = min(g1, t + max_len)
                if g1 - e < min_len:
                    e = g1
                segs.append((b["index"], "background", t, e))
                t = e
    return segs


def condition_string(block):
    c = block.get("conditions", {})
    parts = []
    if "position_m" in c:
        parts.append(f"{c['position_m']}m")
    if "tv" in c:
        parts.append(f"tv-{c['tv']}")
    for k in sorted(c):
        if k not in ("position_m", "tv"):
            parts.append(f"{k}-{c[k]}")
    return "_".join(str(p) for p in parts) or block["name"]


# ---------------------------------------------------------------------------------------------
# alignment: GCC-PHAT in windows, then a robust linear fit offset(t) = a + b * t
# ---------------------------------------------------------------------------------------------


def gcc_phat(ref, sig, lag_samples, lo_hz=150.0, hi_hz=4000.0, excl=32):
    """Cross-correlate `ref` (W samples) against `sig` (W + 2 * lag_samples samples) with PHAT
    weighting, band-limited. Returns (lag_s, confidence): `sig[lag_samples + lag : ... + W]` matches
    `ref`. Confidence is the peak-to-sidelobe ratio (peak - mean) / std of the other lags: about 4-5
    for unrelated signals, far higher for a real match."""
    W = len(ref)
    n = 1 << int(np.ceil(np.log2(len(sig) + W)))
    R = np.fft.rfft(ref.astype(np.float64) * np.hanning(W), n)
    S = np.fft.rfft(sig.astype(np.float64), n)
    G = S * np.conj(R)
    G /= np.abs(G) + 1e-9 * (np.abs(G).max() + 1e-30)
    f = np.fft.rfftfreq(n, 1.0 / RATE)
    G *= ((f >= lo_hz) & (f <= hi_hz))
    cc = np.fft.irfft(G, n)[:2 * lag_samples + 1]
    k = int(np.argmax(cc))
    peak = cc[k]
    mask = np.ones(len(cc), bool)
    mask[max(0, k - excl):k + excl + 1] = False
    rest = cc[mask]
    conf = float((peak - rest.mean()) / (rest.std() + 1e-12)) if len(rest) > 10 else 0.0
    frac = 0.0
    if 0 < k < len(cc) - 1:  # parabolic interpolation of the peak
        a, b, c = cc[k - 1], cc[k], cc[k + 1]
        den = a - 2 * b + c
        frac = 0.5 * (a - c) / den if den != 0 else 0.0
    return (k + frac - lag_samples) / RATE, conf


def _segment(sig, start, length):
    out = np.zeros(length, np.float32)
    a, b = max(0, start), min(len(sig), start + length)
    if b > a:
        out[a - start:b - start] = sig[a:b]
    return out


def _fit_line(taus, ds, iters=4, floor_s=0.005):
    taus, ds = np.asarray(taus, float), np.asarray(ds, float)
    keep = np.ones(len(taus), bool)
    a, b = float(np.median(ds)), 0.0
    if len(taus) < 3 or np.ptp(taus) < 1.0:
        return a, b, keep
    for _ in range(iters):
        b, a = np.polyfit(taus[keep], ds[keep], 1)
        res = ds - (a + b * taus)
        mad = np.median(np.abs(res[keep] - np.median(res[keep]))) * 1.4826
        new = np.abs(res) <= max(3 * mad, floor_s)
        if new.sum() < 3 or (new == keep).all():
            break
        keep = new
    return float(a), float(b), keep


def smooth_curve(taus, ds, max_slope=0.01, max_gap_s=20.0, max_jump_s=1.0):
    """True when the confident window offsets form one continuous curve d(tau): neighbours less than
    `max_gap_s` apart differ by at most `max_slope` x their distance (1 % clock slope, far more than any
    crystal; a dropout of the reference recorder shows up as a short steep stretch), and any larger gap
    by at most `max_jump_s`. A sequence of unrelated peaks cannot pass this: it is not monotone-smooth."""
    order = np.argsort(taus)
    t, d = np.asarray(taus, float)[order], np.asarray(ds, float)[order]
    if len(t) < 6:
        return False
    dt, dd = np.diff(t), np.abs(np.diff(d))
    return bool(np.all(np.where(dt <= max_gap_s, dd <= max_slope * dt + 0.003, dd <= max_jump_s)))


def curve_ref_time(curve, a, b, j):
    """Reference time tau of file time j (array or scalar) for an alignment: piecewise-linear
    d(tau) through `curve` ([[tau, d], ...], flat beyond its ends with the drift b) or the line a + b * tau.
    Solves j = tau + d(tau) by fixed-point iteration (the slope of d is far below 1)."""
    j = np.asarray(j, float)
    if not curve:
        return (j - a) / (1 + b)
    ct, cd = np.asarray(curve, float).T

    def d_of(tau):
        d = np.interp(tau, ct, cd)
        return np.where(tau < ct[0], cd[0] + b * (tau - ct[0]), np.where(tau > ct[-1], cd[-1] + b * (tau - ct[-1]), d))

    tau = j - d_of(j - cd[0])
    for _ in range(4):
        tau = j - d_of(tau)
    return tau


def curve_offset(curve, a, b, tau):
    """d(tau) of an alignment record (see `curve_ref_time`)."""
    if not curve:
        return a + b * np.asarray(tau, float)
    ct, cd = np.asarray(curve, float).T
    tau = np.asarray(tau, float)
    return np.where(tau < ct[0], cd[0] + b * (tau - ct[0]), np.where(tau > ct[-1], cd[-1] + b * (tau - ct[-1]),
                                                                      np.interp(tau, ct, cd)))


REF_MAP_MIN_S = 0.3


def session_to_ref(session, min_dev_s=REF_MAP_MIN_S):
    """(f, mode): f maps session time to the desk-mic file's time (tau). Normally tau = t - first_sample_offset.
    When the desk recorder lost more than `min_dev_s` of audio during the session (a dropout; the
    session's `progress` list holds (elapsed s, samples so far) every 10 s), the mapping follows
    the recorded sample count instead, so cues stay on the audio they were spoken in. A smaller
    drift is left alone (the cue windows are 4 s wide)."""
    o = session_pc_offset(session)
    pr = ((session.get("sources") or {}).get("pc-mic") or {}).get("progress") or []
    devs = [(float(e), float(e) - (o + float(n) / RATE)) for e, n in pr]
    if len(devs) < 3 or max(d for _, d in devs) - min(d for _, d in devs) < min_dev_s:
        return (lambda t: np.asarray(t, float) - o), "offset"
    es = np.array([e for e, _ in devs])
    ds = np.array([d for _, d in devs])
    return (lambda t: np.asarray(t, float) - o - np.interp(t, es, ds, left=0.0, right=ds[-1])), "progress"


def align_stream(ref, sig, d_prior_s, *, coarse_lag_s=20.0, refine_lag_s=1.5, win_s=15.0, min_conf=8.0,
                 max_coarse_windows=60, min_rms=20.0, max_resid_s=0.03, max_drift=1e-3, allow_curve=False):
    """Offset of `sig` against `ref` (both int16 at 16 kHz): `sig` time = `ref` time + d(tau), tau in
    seconds of `ref`. Starts from the prior d_prior_s (for the TV file: from its mtime). Stage 1
    finds the offset on the windows with the most energy in both signals within +-coarse_lag_s; stage 2 measures every window
    within +-refine_lag_s of that and fits d(tau) = a + b * tau (b is the drift).
    Returns a dict with a, b, ok, confidence statistics and the per-window results. When `ok` is
    False the offset is the prior (nothing measured can be trusted)."""
    W = int(win_s * RATE)
    ref_f = ref.astype(np.float32)
    sig_f = sig.astype(np.float32)
    starts = []
    for s in range(0, len(ref) - W + 1, W):
        rms = float(np.sqrt(np.mean(ref_f[s:s + W] ** 2)))
        j0 = int(round((s / RATE + d_prior_s) * RATE))
        overlap = min(len(sig), j0 + W) - max(0, j0)
        srms = float(np.sqrt(np.mean(_segment(sig_f, j0, W) ** 2)))
        if rms >= min_rms and srms >= min_rms and overlap >= W // 2:
            starts.append((s, min(rms, srms)))
    dur = len(ref) / RATE
    res = {"a": float(d_prior_s), "b": 0.0, "ok": False, "n_windows": len(starts), "n_coarse": 0, "n_confident": 0,
           "n_inliers": 0, "median_conf": 0.0, "resid_std_ms": None, "drift_ppm": 0.0,
           "drift_over_session_s": 0.0, "d_prior_s": float(d_prior_s), "windows": []}
    if not starts:
        return res

    def measure(s, d0, lag_s):
        L = int(lag_s * RATE)
        j0 = int(round((s / RATE + d0) * RATE))
        lag, conf = gcc_phat(ref_f[s:s + W], _segment(sig_f, j0 - L, W + 2 * L), L)
        return s / RATE + win_s / 2, d0 + lag, conf

    # every window is tried (best first, at most max_coarse_windows): windows are ranked by the
    # weaker of the two signals, so a loud TV that only one recorder hears does not crowd out the
    # windows where both recorders hear the speaker
    best = sorted(starts, key=lambda x: -x[1])[:max_coarse_windows]
    res["n_coarse"] = len(best)
    coarse = [measure(s, d_prior_s, coarse_lag_s) for s, _ in best]
    good = [m for m in coarse if m[2] >= min_conf]
    if not good:
        res["median_conf"] = float(np.median([m[2] for m in coarse]))
        res["windows"] = [{"tau_s": round(t, 2), "d_s": round(d, 5), "conf": round(c, 2)} for t, d, c in coarse]
        return res
    d_med = float(np.median([m[1] for m in good]))
    fine = [measure(s, d_med, refine_lag_s) for s, _ in starts]
    conf_w = [m for m in fine if m[2] >= min_conf]
    res["windows"] = [{"tau_s": round(t, 2), "d_s": round(d, 5), "conf": round(c, 2)} for t, d, c in fine]
    res["n_confident"] = len(conf_w)
    res["median_conf"] = float(np.median([m[2] for m in fine]))
    if not conf_w:
        return res
    taus = [m[0] for m in conf_w]
    ds = [m[1] for m in conf_w]
    a, b, keep = _fit_line(taus, ds)
    resid = np.asarray(ds) - (a + b * np.asarray(taus))
    resid_std = float(resid[keep].std())
    # A match is trusted only when the confident windows agree with one offset line: enough of them,
    # a small residual, and a physically plausible clock drift (an unrelated pair gives scattered offsets).
    ok = (int(keep.sum()) >= 2 and keep.sum() >= 0.5 * len(conf_w) and resid_std <= max_resid_s
          and abs(b) <= max_drift)
    res.update(ok=bool(ok), n_inliers=int(keep.sum()), resid_std_ms=round(resid_std * 1000, 3))
    all_resid = float(np.std(np.asarray(ds) - (a + b * np.asarray(taus))))
    if (allow_curve and len(conf_w) >= max(6, 0.8 * len(fine)) and all_resid > max_resid_s and smooth_curve(taus, ds)):
        # one offset line does not fit all the windows (it may fit a subset, by dropping the rest as outliers), yet
        # nearly every window found a sharp peak and the offsets form a smooth curve: the clocks did not run at a
        # constant ratio (a dropout or creep in one recorder). Keep the curve.
        o = np.argsort(taus)
        curve = [[round(float(taus[i]), 3), round(float(ds[i]), 5)] for i in o]
        res.update(ok=True, curve=curve, mode="piecewise", a=a, b=b, drift_ppm=round(b * 1e6, 2),
                   drift_over_session_s=round(float(max(ds) - min(ds)), 4), n_inliers=len(conf_w),
                   resid_std_ms=round(all_resid * 1000, 3))
    elif ok:
        res.update(a=a, b=b, drift_ppm=round(b * 1e6, 2), drift_over_session_s=round(b * dur, 4))
    return res


def align_short(ref, sig, d_prior_s, *, lag_s=3.0, min_conf=8.0, min_rms=20.0):
    """Offset of one SHORT file (a request-audio run of 1-15 s) against `ref`, with the same
    convention as `align_stream` (`sig` time = `ref` time + d). The whole file is the template, and
    the search is over +-`lag_s` around its prior position in `ref` (so every lag has full overlap).
    One window cannot show a consistent offset line, so `ok` here only says the single peak is
    confident (PSR >= `min_conf`, not on the search edge); the caller must still check the files
    of a group against each other (`consistent_group`). The drift is not estimated (b = 0): over a
    file of 15 s at the 130 ppm seen on the TV it is 2 ms."""
    W, L = len(sig), int(lag_s * RATE)
    res = {"a": float(d_prior_s), "b": 0.0, "ok": False, "n_windows": 1, "n_coarse": 1, "n_confident": 0,
           "n_inliers": 0, "median_conf": 0.0, "resid_std_ms": None, "drift_ppm": 0.0,
           "drift_over_session_s": 0.0, "d_prior_s": float(d_prior_s), "windows": [], "lag_s": None}
    if W < RATE // 2:
        return res
    sig_f, ref_f = sig.astype(np.float32), ref.astype(np.float32)
    j0 = int(round(-d_prior_s * RATE))                  # position of the file's sample 0 in ref
    seg = _segment(ref_f, j0 - L, W + 2 * L)
    srms = float(np.sqrt(np.mean(sig_f ** 2)))
    rrms = float(np.sqrt(np.mean(seg[L:L + W] ** 2)))
    lag, conf = gcc_phat(sig_f, seg, L)                  # ref[j0 + lag ...] matches the file's start
    res["median_conf"] = float(conf)
    res["lag_s"] = float(lag)
    res["windows"] = [{"tau_s": round((j0 / RATE) + lag + W / RATE / 2, 2), "d_s": round(d_prior_s - lag, 5),
                       "conf": round(float(conf), 2)}]
    if srms < min_rms or rrms < min_rms:
        return res
    res.update(n_confident=int(conf >= min_conf), a=float(d_prior_s - lag))
    res["ok"] = bool(conf >= min_conf and abs(lag) < lag_s - 0.05)
    res["n_inliers"] = int(res["ok"])
    return res


def consistent_group(results, tol_s=0.5, min_files=2):
    """Which short files agree with each other. `results` are `align_short` dicts (prior and offset);
    each file's offset error against the listed start (`a - d_prior`) is the same latency for all
    files of one device (the file clock is the Home Assistant host), so confident files must agree
    within `tol_s` of their median. Returns a list of booleans: confident AND consistent, and at
    least `min_files` such files (a lone file has nothing to agree with, so it is not trusted)."""
    conf = [r["ok"] for r in results]
    deltas = [r["a"] - r["d_prior_s"] for r in results]
    good = [d for d, c in zip(deltas, conf) if c]
    if len(good) < min_files:
        return [False] * len(results)
    med = float(np.median(good))
    out = [bool(c and abs(d - med) <= tol_s) for d, c in zip(deltas, conf)]
    return out if sum(out) >= min_files else [False] * len(results)
