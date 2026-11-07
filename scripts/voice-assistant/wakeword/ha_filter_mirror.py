#!/usr/bin/env python3
"""Replay the real Kivi data through a Home Assistant side noise filter (offline, no retrain).

Question (webos-voice-satellite `docs/research/ha-side-audio-processing.md`, section 9): would a filter
that Home Assistant applied after the satellite and before the wake word change recall and false
accepts? HA's debug recordings (the raw wake files and the room negatives of the dataset) are the wake
word's exact input, so a filtered copy of them is exactly what the wake word would see behind the filter.

`build` mirrors the dataset tree into `--dst` (normally `datasets/derived/<filter>/`):

- every satellite raw wake file (`<session>/tv-*/raw/*.wav`) and every room negative
  (`negatives/<device>/*.wav`) is filtered in pieces of 10 min, each preceded by 10 s of its own audio
  that is filtered and dropped (adaptation; some negatives are 6 h long), delay-compensated (the
  filter's latency is measured with a chirp burst and removed, the output has the input's length), and
  written as a new WAV (mode 600, `.part` until complete);
- every other file (manifests, marks, alignment, session and extract records, the PC sources, the
  clips) is a symbolic link to the original, so `kivi_score.py` and `v3_room.py` run unchanged on the
  mirror; `scores/` directories are created empty, never linked, so no score lands in the original;
- `derived.json` records the provenance (source, filter, parameters, tool sha256, latency) and the
  aggregates only: file count, hours, CPU seconds per audio hour, RMS change percentiles, and how many
  files still show a residual delay. No audio, no per-file listing, no file names.

Filters (input and output: the satellite's 16 kHz mono S16 stream):

- `webrtc-ns`: a second WebRTC NS pass: `aec_offline` (webos-voice-satellite `crates/capture/examples`)
  with a silent reference (AEC3 then passes the signal through), `--ns --gain-db 0 --hpf-hz 0`.
  Its CPU figure includes the pass-through AEC3.
- `rnnoise`: `aec_offline --no-aec --rnnoise --gain-db 0 --hpf-hz 0` (RNNoise at 48 kHz, resampled).
- `speex`: Home Assistant's own enhancer, `pyspeex_noise.AudioProcessor(auto_gain * 300,
  level * -15)` on 10 ms frames, as `MicroVadSpeexEnhancer` builds it (`--speex-level 2`, AGC off).

`verify` re-checks an existing mirror (lengths, and the residual delay of every 10 min piece on its
loudest 10 s; pieces the filter made more than 30 dB quieter have nothing left to align on and are
counted as unverifiable). `bench` measures the CPU of the filter calls alone on the first `--hours`
of the targets. `split` rewrites a `v3_room.py split` JSON so that it points at the mirror's negatives:

    ha_filter_mirror.py build --src $W/datasets/real --dst $W/datasets/derived/rnnoise --filter rnnoise \
        --tool $W/v2/fe_build/release/examples/aec_offline
    ha_filter_mirror.py split --split $W/v3/room_split.json --dst $W/datasets/derived/rnnoise

Re-running `build` skips files that are already complete, so an interrupted run resumes. Run it under
the PC caps (`systemd-run --user --scope -p MemoryMax=6G -p CPUQuota=400% nice -n 10`). The speex
filter needs `pyspeex-noise` (HA 2026.10.4 ships 1.0.2), installed in a separate venv.
"""
import os

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "4")  # the PC is shared

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import resource  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kivi_real_common as C  # noqa: E402

RATE = 16000
FRAME = 160
FILTERS = ("none", "webrtc-ns", "rnnoise", "speex")
MAX_LAG = 2048          # samples searched for a filter's delay
CHECK_LAG = 400         # residual delay search on real files (+-25 ms)
LAG_TOL = 2             # samples (0.125 ms): a filter's phase response can move the peak by one
PIECE_S = 600           # long files are filtered in 10 min pieces (memory: some files are 6 h long) ...
PREROLL_S = 10          # ... each preceded by 10 s of its own audio, filtered and discarded (adaptation)
SILENCED_DB = -30       # a piece the filter turned this much quieter has no structure left to align on


# ---------------------------------------------------------------------------------------------
# filters: int16 array in, int16 array of the same length out (delay removed)
# ---------------------------------------------------------------------------------------------

class SpeexFilter:
    """HA's MicroVadSpeexEnhancer audio path (VAD left out: it does not change the audio)."""

    name = "speex"

    def __init__(self, level=2, auto_gain=0):
        from pyspeex_noise import AudioProcessor
        self._cls = AudioProcessor
        self.level, self.auto_gain = int(level), int(auto_gain)
        if not (0 <= self.level <= 4 and 0 <= self.auto_gain <= 31):
            raise ValueError("speex level must be in [0, 4] and auto gain in [0, 31]")
        self.params = {"noise_suppression_level": self.level, "noise_suppression_db": self.level * -15,
                       "auto_gain_dbfs": self.auto_gain, "speex_auto_gain_arg": self.auto_gain * 300,
                       "frame_samples": FRAME}
        self.lag = 0

    def raw(self, x):
        p = self._cls(self.auto_gain * 300, self.level * -15)   # a new processor per stream, as HA per run
        n = -(-len(x) // FRAME) * FRAME
        buf = np.zeros(n, "<i2")
        buf[:len(x)] = x
        out = [p.Process10ms(buf[i:i + FRAME].tobytes()).audio for i in range(0, n, FRAME)]
        return np.frombuffer(b"".join(out), "<i2")


class AecOfflineFilter:
    """The satellite repository's `aec_offline` file mode as a pure filter (silent reference)."""

    def __init__(self, tool, mode, tmpdir=None):
        self.tool, self.mode, self.tmpdir = str(tool), mode, tmpdir
        self.name = mode
        self.args = {"webrtc-ns": ["--ns"], "rnnoise": ["--no-aec", "--rnnoise"]}[mode]
        self.params = {"tool": self.tool, "tool_sha256": sha256_file(self.tool),
                       "args": ["--gain-db", "0", "--hpf-hz", "0"] + self.args}
        self.lag = 0
        self.last = None

    def raw(self, x):
        with tempfile.TemporaryDirectory(dir=self.tmpdir) as d:
            mic, ref, out = (os.path.join(d, n) for n in ("mic.raw", "ref.raw", "out.wav"))
            (np.asarray(x, np.int32) * 256).astype("<i4").tofile(mic)   # S24 in 4-byte containers, gain 0 dB
            np.zeros(len(x), "<i4").tofile(ref)
            cmd = [self.tool, "--mic", mic, "--ref", ref, "--out", out, "--mic-channels", "1",
                   "--ref-channels", "1", "--ref-rate", str(RATE), "--ref-format", "s24_le", "--skip-ms", "0",
                   "--gain-db", "0", "--hpf-hz", "0"] + self.args
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if r.returncode != 0:
                raise RuntimeError(f"aec_offline exited {r.returncode}: {r.stderr[-400:]}")
            try:
                self.last = json.loads(r.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                self.last = None
            y = np.fromfile(out, dtype="<i2", offset=44)
        if self.last is not None and self.last.get("in_samples") != len(x):
            raise RuntimeError(f"aec_offline read {self.last.get('in_samples')} samples, expected {len(x)}")
        return y


class NoFilter:
    name = "none"
    params = {}
    lag = 0

    def raw(self, x):
        return np.asarray(x, "<i2")


def make_filter(name, tool=None, speex_level=2, tmpdir=None):
    if name == "none":
        return NoFilter()
    if name == "speex":
        return SpeexFilter(speex_level)
    if name in ("webrtc-ns", "rnnoise"):
        if not tool or not Path(tool).is_file():
            raise SystemExit(f"--tool (aec_offline) is required for {name}")
        return AecOfflineFilter(tool, name, tmpdir)
    raise SystemExit(f"unknown filter {name}")


def best_lag(x, y, lo, hi):
    """Lag k in [lo, hi] maximising |<x, y shifted by k>| (y[n + k] ~ x[n]), by FFT correlation."""
    x = np.asarray(x, np.float64)
    y = np.asarray(y, np.float64)
    n = len(x) + len(y)
    size = 1 << (n - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(y, size) * np.conj(np.fft.rfft(x, size)), size)
    lags = np.arange(lo, hi + 1)
    return int(lags[np.argmax(np.abs(c[lags % size]))])


def chirp_probe(seed=1):
    """4 s: a quiet noise floor with a 0.25 s 200-4000 Hz chirp at 1.5 s (no recorded audio)."""
    rng = np.random.default_rng(seed)
    x = 3.0 * rng.standard_normal(4 * RATE)
    t = np.arange(4000) / RATE
    x[24000:28000] += 8000 * np.sin(2 * np.pi * (200 * t + 7600 * t ** 2)) * np.hanning(4000)
    return np.clip(np.round(x), -32768, 32767).astype("<i2")


def measure_lag(f):
    x = chirp_probe()
    y = f.raw(x)
    return best_lag(x[20000:32000], y[20000:20000 + 12000 + MAX_LAG], 0, MAX_LAG)


def apply(f, x):
    """Filter a whole stream; remove the measured delay; keep the length."""
    x = np.asarray(x, "<i2")
    # the delay plus up to two 10 ms frames of zeros: some paths drop a partial last frame
    padded = np.concatenate([x, np.zeros(f.lag + 2 * FRAME, "<i2")])
    y = f.raw(padded)
    y = y[f.lag:f.lag + len(x)]
    if len(y) != len(x):
        raise RuntimeError(f"filter output {len(y)} samples, expected {len(x)}")
    return np.ascontiguousarray(y, "<i2")


def residual_lag(x, y, win=10 * RATE):
    """Delay between input and output on the loudest 10 s window (0 when compensated), or None."""
    if len(x) < win + 2 * CHECK_LAG:
        return None
    n_s = len(x) // RATE
    e = np.sum(np.asarray(x[:n_s * RATE], np.float64).reshape(n_s, RATE) ** 2, axis=1)   # energy per second
    w = win // RATE
    e10 = np.convolve(e, np.ones(w), "valid")                                              # per 10 s window
    s = int(np.argmax(e10)) * RATE
    s = min(max(s, CHECK_LAG), len(x) - win - CHECK_LAG)
    return best_lag(x[s:s + win], y[s - CHECK_LAG:s + win + CHECK_LAG], 0, 2 * CHECK_LAG) - CHECK_LAG


def filter_file(f, x, writer, piece_s=PIECE_S, preroll_s=PREROLL_S, check=True):
    """Filter `x` piece by piece into `writer`; returns aggregates (no audio is kept).

    Each piece is preceded by `preroll_s` of the audio before it, filtered with it and then dropped, so
    the filter has adapted when the kept part starts (a fresh filter state per piece)."""
    n, piece, pre = len(x), int(piece_s * RATE), int(preroll_s * RATE)
    agg = {"ss_in": 0.0, "ss_out": 0.0, "checked": 0, "nonzero": 0, "unverifiable": 0, "cpu_s": 0.0}
    for p in range(0, n, piece):
        s, e = max(0, p - pre), min(n, p + piece)
        seg = np.asarray(x[s:e], "<i2")
        c0, u0 = time.process_time(), resource.getrusage(resource.RUSAGE_CHILDREN)
        y = apply(f, seg)[p - s:]
        u1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        agg["cpu_s"] += (time.process_time() - c0) + (u1.ru_utime - u0.ru_utime) + (u1.ru_stime - u0.ru_stime)
        kept = seg[p - s:]
        if writer is not None:
            writer.write(np.ascontiguousarray(y, "<i2").tobytes())
        agg["ss_in"] += float(np.sum(kept.astype(np.int64) ** 2))
        agg["ss_out"] += float(np.sum(y.astype(np.int64) ** 2))
        if check:
            check_piece(kept, y, agg)
    return agg


def check_piece(x, y, agg, tol=LAG_TOL):
    """Residual delay of one piece; pieces the filter silenced cannot be checked and are counted apart."""
    if rms_db(y) - rms_db(x) < SILENCED_DB:
        agg["unverifiable"] += 1
        return
    lag = residual_lag(x, y)
    if lag is not None:
        agg["checked"] += 1
        agg["nonzero"] += int(abs(lag) > tol)


def rms_db(x):
    v = float(np.mean(np.asarray(x, np.float64) ** 2))
    return 10 * np.log10(v) if v > 0 else -200.0


# ---------------------------------------------------------------------------------------------
# mirror
# ---------------------------------------------------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def is_target(rel):
    """True for the wake word's input files: satellite raw wake files and room negatives."""
    p = rel.parts
    if not rel.name.endswith(".wav"):
        return False
    if len(p) == 3 and p[0] == "negatives":
        return True
    return len(p) == 4 and p[1].startswith("tv-") and p[2] == "raw"


def plan(src):
    """(dirs, links, targets) relative to src; `scores` and `levels` subtrees and `.part` files are skipped."""
    src = Path(src)
    dirs, links, targets = [], [], []
    for d, dn, fn in os.walk(src):
        rel = Path(d).relative_to(src)
        dn[:] = sorted(x for x in dn if x not in ("scores", "levels"))
        dirs.append(rel)
        for name in sorted(fn):
            if ".part" in name:
                continue
            r = rel / name
            (targets if is_target(r) else links).append(r)
    return dirs, links, targets


def build(a):
    src, dst = Path(a.src).resolve(), Path(a.dst).resolve()
    if dst == src or src in dst.parents:
        raise SystemExit("--dst must be outside --src")
    f = make_filter(a.filter, a.tool, a.speex_level, a.tmp)
    f.lag = measure_lag(f)
    dirs, links, targets = plan(src)
    C.private_mkdir(dst, dst.parent)
    for d in dirs:
        C.private_mkdir(dst / d, dst)
        if (src / d / "scores").is_dir():
            C.private_mkdir(dst / d / "scores", dst)
    for r in links:
        t = dst / r
        if t.is_symlink() or t.exists():
            continue
        os.symlink(src / r, t)
    done = skipped = 0
    secs = cpu = 0.0
    deltas, residual = [], {"checked": 0, "nonzero": 0, "unverifiable": 0}
    for i, r in enumerate(targets):
        t = dst / r
        if t.exists() and not t.is_symlink():
            skipped += 1
            continue
        x = C.read_pcm16(src / r)
        if len(x) == 0:
            skipped += 1
            continue
        w = C.PartWavWriter(t)
        agg = filter_file(f, x, w, check=a.filter != "none")
        cpu += agg["cpu_s"]
        w.commit(expected_bytes=2 * len(x))
        os.chmod(t, 0o600)
        secs += len(x) / RATE
        if agg["ss_in"] > 0 and agg["ss_out"] > 0:
            deltas.append(10 * np.log10(agg["ss_out"] / agg["ss_in"]))
        residual["checked"] += agg["checked"]
        residual["nonzero"] += agg["nonzero"]
        residual["unverifiable"] += agg["unverifiable"]
        done += 1
        print(f"{i + 1}/{len(targets)}", file=sys.stderr, flush=True)
    hours = secs / 3600
    prev = {}
    rec = dst / "derived.json"
    if rec.exists():
        prev = json.loads(rec.read_text(encoding="utf-8"))
    runs = prev.get("runs", []) + [{
        "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "files_written": done,
        "files_skipped": skipped, "hours_written": round(hours, 3), "cpu_s": round(cpu, 1),
        "cpu_s_per_audio_hour": round(cpu / hours, 1) if hours else None,
        "rms_change_db": ({"p5": round(float(np.percentile(deltas, 5)), 2),
                           "median": round(float(np.median(deltas)), 2),
                           "p95": round(float(np.percentile(deltas, 95)), 2)} if deltas else None),
        "residual_delay": residual | {"tolerance_samples": LAG_TOL, "silenced_db": SILENCED_DB}}]
    out = {"what": "HA-side filter replay of the Kivi real dataset (derived audio; see ha_filter_mirror.py)",
           "source": str(src), "filter": a.filter, "params": f.params, "latency_removed_samples": f.lag,
           "pieces": {"piece_s": PIECE_S, "preroll_s": PREROLL_S},
           "targets": len(targets), "linked_files": len(links), "runs": runs}
    rec.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    os.chmod(rec, 0o600)
    print(json.dumps({k: out[k] for k in ("filter", "latency_removed_samples", "targets")} | runs[-1]))
    if residual["nonzero"] > 0.05 * max(1, residual["checked"]):  # `verify` re-checks an existing mirror
        raise SystemExit(f"{residual['nonzero']} of {residual['checked']} files show a residual delay")


def update_record(dst, key, value):
    rec = Path(dst) / "derived.json"
    d = json.loads(rec.read_text(encoding="utf-8"))
    d.setdefault(key, []).append(value)
    rec.write_text(json.dumps(d, indent=1) + "\n", encoding="utf-8")


def verify(a):
    """Re-check the residual delay of an existing mirror, piece by piece (reads both trees)."""
    src, dst = Path(a.src).resolve(), Path(a.dst).resolve()
    _, _, targets = plan(src)
    agg = {"checked": 0, "nonzero": 0, "unverifiable": 0, "missing": 0, "length_mismatch": 0}
    piece = PIECE_S * RATE
    for i, r in enumerate(targets):
        if not (dst / r).is_file() or (dst / r).is_symlink():
            agg["missing"] += 1
            continue
        x, y = C.read_pcm16(src / r), C.read_pcm16(dst / r)
        if len(x) != len(y):
            agg["length_mismatch"] += 1
            continue
        for p in range(0, len(x), piece):
            check_piece(x[p:p + piece], y[p:p + piece], agg, a.lag_tol)
        print(f"{i + 1}/{len(targets)}", file=sys.stderr, flush=True)
    v = {"utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "targets": len(targets),
         "tolerance_samples": a.lag_tol, "silenced_db": SILENCED_DB} | agg
    update_record(dst, "verify", v)
    print(json.dumps(v))
    if agg["missing"] or agg["length_mismatch"] or agg["nonzero"] > 0.05 * max(1, agg["checked"]):
        raise SystemExit("verify failed")


def bench(a):
    """CPU of the filter calls alone (no writing, no checks) on the first `--hours` of the targets."""
    src = Path(a.src).resolve()
    f = make_filter(a.filter, a.tool, a.speex_level, a.tmp)
    f.lag = measure_lag(f)
    _, _, targets = plan(src)
    secs = cpu = 0.0
    for r in targets:
        if secs >= a.hours * 3600:
            break
        x = C.read_pcm16(src / r)
        agg = filter_file(f, x, None, check=False)
        secs += len(x) / RATE
        cpu += agg["cpu_s"]
    v = {"utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "filter": a.filter,
         "audio_hours": round(secs / 3600, 3), "cpu_s": round(cpu, 1),
         "cpu_s_per_audio_hour": round(cpu / (secs / 3600), 1) if secs else None,
         "includes": "the filter call per piece (speex: the Python frame loop; aec_offline: S24 temp files, the "
                     "process, reading its output), no dataset writing, no checks"}
    print(json.dumps(v))
    if a.dst:
        update_record(a.dst, "bench", v)


def split(a):
    sp = json.loads(Path(a.split).read_text(encoding="utf-8"))
    dst = Path(a.dst).resolve()
    if not (dst / "negatives").is_dir():
        raise SystemExit(f"{dst}/negatives is missing: run build first")
    sp["negatives"] = str(dst / "negatives")
    sp["derived_from"] = str(Path(a.split).resolve())
    out = Path(a.out) if a.out else dst / "room_split.json"
    out.write_text(json.dumps(sp, indent=1), encoding="utf-8")
    os.chmod(out, 0o600)
    print(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--src", default=C.DEFAULT_DATASET_ROOT)
    b.add_argument("--dst", required=True)
    b.add_argument("--filter", required=True, choices=FILTERS)
    b.add_argument("--tool", help="aec_offline binary (webrtc-ns, rnnoise)")
    b.add_argument("--speex-level", type=int, default=2, help="HA noise_suppression_level, 0-4 (speex)")
    b.add_argument("--tmp", help="directory for aec_offline's temporary files (default: system temp)")
    v = sub.add_parser("verify")
    v.add_argument("--src", default=C.DEFAULT_DATASET_ROOT)
    v.add_argument("--dst", required=True)
    v.add_argument("--lag-tol", type=int, default=LAG_TOL,
                   help="samples; the second WebRTC NS pass shows 0 to 14 (under 1 ms), far below the 80 ms scoring step")
    bn = sub.add_parser("bench")
    bn.add_argument("--src", default=C.DEFAULT_DATASET_ROOT)
    bn.add_argument("--filter", required=True, choices=FILTERS)
    bn.add_argument("--tool")
    bn.add_argument("--speex-level", type=int, default=2)
    bn.add_argument("--tmp")
    bn.add_argument("--hours", type=float, default=2.0)
    bn.add_argument("--dst", help="append the result to this mirror's derived.json")
    s = sub.add_parser("split")
    s.add_argument("--split", required=True, help="v3_room.py split JSON of the original negatives")
    s.add_argument("--dst", required=True, help="the mirror built by `build`")
    s.add_argument("--out", help="default: <dst>/room_split.json")
    a = ap.parse_args(argv)
    {"build": build, "split": split, "verify": verify, "bench": bench}[a.cmd](a)


if __name__ == "__main__":
    main()
