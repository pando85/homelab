"""Tests of the real-voice tools. Synthetic signals only: no microphone, no real recording.

    cd scripts/voice-assistant/wakeword && OMP_NUM_THREADS=4 \
        /work/workspaces/kivi-wakeword/.venv/bin/python -m pytest test_kivi_real.py -q

The scorer tests use the synthetic held-out Piper clips under /work/workspaces/kivi-wakeword and are
skipped when that work directory is absent.
"""
import json
import os

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "4")

import csv  # noqa: E402
import re  # noqa: E402
import stat  # noqa: E402
import sys  # noqa: E402
from argparse import Namespace  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import kivi_extract as X  # noqa: E402
import kivi_import_phone as P  # noqa: E402
import kivi_negatives as N  # noqa: E402
import kivi_real_common as C  # noqa: E402
import kivi_session as S  # noqa: E402

RATE = C.RATE
WORK = Path(os.environ.get("WORK", "/work/workspaces/kivi-wakeword"))


def speechlike(seconds, seed):
    """Bursts of band-limited noise with random envelopes: enough structure for GCC-PHAT."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    x = rng.standard_normal(n)
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1 / RATE)
    spec[(f < 200) | (f > 3500)] = 0
    x = np.fft.irfft(spec, n)
    env = np.repeat(rng.random(int(seconds / 0.25) + 2) ** 2, int(0.25 * RATE))[:n]
    x = x * env
    return (x / np.abs(x).max() * 9000).astype(np.int16)


def delayed(ref, d_s, rng, gain=6.0, snr_db=12, ppm=0.0, tail_s=0.0):
    """sig time = ref time + d: the other recorder's version of `ref`, filtered, noisy, clock-drifted."""
    k = int(round(d_s * RATE))
    n = len(ref) + k + int(tail_s * RATE)
    x = np.zeros(n, np.float32)
    x[k:k + len(ref)] = ref
    ir = np.array([1.0, 0.0, 0.35, 0.0, 0.0, -0.2, 0.1], np.float32)
    x = np.convolve(x, ir, mode="same") * gain
    if ppm:
        t = np.arange(n) * (1 + ppm * 1e-6)
        x = np.interp(t, np.arange(n), x, right=0.0)
    p = np.sqrt(np.mean(ref.astype(np.float32) ** 2)) * gain
    x = x + rng.standard_normal(n) * p / 10 ** (snr_db / 20)
    return np.clip(x, -32000, 32000).astype(np.int16)


# ---------------------------------------------------------------------------------------------
# alignment
# ---------------------------------------------------------------------------------------------


def test_alignment_recovers_known_offset_from_a_wrong_prior():
    rng = np.random.default_rng(1)
    ref = speechlike(100, 11)
    d_true = 3.2173
    sig = delayed(ref, d_true, rng)
    al = C.align_stream(ref, sig, d_true + 1.7)  # mtime estimate off by 1.7 s
    assert al["ok"] and al["n_confident"] >= 5
    assert abs(al["a"] - d_true) < 0.002, al["a"]
    assert al["median_conf"] > 12
    assert abs(al["drift_ppm"]) < 5


def test_alignment_reports_drift():
    rng = np.random.default_rng(2)
    ref = speechlike(120, 12)
    sig = delayed(ref, 2.0, rng, ppm=80.0)
    al = C.align_stream(ref, sig, 2.4)
    assert al["ok"]
    # sampling `sig` at (1 + 80 ppm) x its time axis makes its content arrive earlier and earlier
    assert abs(al["drift_ppm"] + 80.0) < 10, al["drift_ppm"]
    assert abs(al["drift_over_session_s"] + 80e-6 * 120) < 0.002


def test_alignment_of_unrelated_audio_is_not_trusted():
    ref = speechlike(60, 13)
    other = speechlike(70, 14)
    al = C.align_stream(ref, other, 0.5)
    assert not al["ok"]            # scattered offsets: not trusted, even if single windows look sharp
    assert al["a"] == 0.5 and al["b"] == 0.0  # falls back to the prior


# ---------------------------------------------------------------------------------------------
# names, dataset helpers, manifest
# ---------------------------------------------------------------------------------------------


def test_device_and_speaker_naming():
    assert C.source_label("c3-a") == "tv-c3a" and C.source_label("c3-b") == "tv-c3b"
    assert C.device_prefix("c3-a") == "bd8256" and C.device_prefix("c3-b") == "fda18f"
    assert C.device_prefix("x1", override="abcd12") == "abcd12"
    with pytest.raises(SystemExit):
        C.device_prefix("nope")
    assert C.valid_speaker("spk01") == "spk01"
    for bad in ("alice", "spk1", "Spk01", "spk01 x", ""):
        with pytest.raises(SystemExit):
            C.valid_speaker(bad)


def test_private_dataset_helpers_and_manifest(tmp_path):
    root = tmp_path / "real"
    d = C.private_mkdir(root / "s1" / "tv-c3a" / "clips", root)
    for p in (root, root / "s1", root / "s1" / "tv-c3a", d):
        assert stat.S_IMODE(p.stat().st_mode) == 0o700
    C.write_wav_new(d / "a.wav", np.arange(100, dtype=np.int16))
    assert stat.S_IMODE((d / "a.wav").stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):  # never overwrites
        C.write_wav_new(d / "a.wav", np.zeros(10, np.int16))
    assert (C.read_pcm16(d / "a.wav") == np.arange(100)).all()
    m = root / "manifest.jsonl"
    C.append_jsonl(m, [{"session": "s1", "clip": "a"}])
    C.append_jsonl(m, [{"session": "s1", "clip": "b"}, {"session": "s2", "clip": "c"}])
    rows = C.read_jsonl(m)
    assert [r["clip"] for r in rows] == ["a", "b", "c"]
    assert stat.S_IMODE(m.stat().st_mode) == 0o600


def test_wake_file_with_placeholder_header_is_read_from_offset_44(tmp_path):
    pcm = (np.arange(1000) % 200 - 100).astype("<i2")
    hdr = C.wav_header(0)
    hdr = hdr[:4] + b"\xff\xff\xff\xff" + hdr[8:40] + b"\xff\xff\xff\xff"  # open file: sizes unknown
    p = tmp_path / "live.wav"
    p.write_bytes(hdr + pcm.tobytes())
    assert (C.read_pcm16(p) == pcm).all()





# ---------------------------------------------------------------------------------------------
# prompter in dry-run
# ---------------------------------------------------------------------------------------------

SMALL_BLOCKS = {"blocks": [
    {"name": "kivi-a", "kind": "kivi", "n": 4, "interval_s": 5, "jitter_s": 1,
     "conditions": {"position_m": 1.5, "tv": "off"}},
    {"name": "near", "kind": "near", "words": ["kiwi", "aquí vi"], "each": 2, "interval_s": 5, "jitter_s": 1,
     "conditions": {"position_m": 1.5, "tv": "off"}},
    {"name": "silence", "kind": "silence", "seconds": 8, "conditions": {"position_m": 1.5, "tv": "off"}}]}


def dry_session(tmp_path, extra=(), blocks=SMALL_BLOCKS, seed=3):
    bf = tmp_path / "blocks.json"
    bf.write_text(json.dumps(blocks), encoding="utf-8")
    out = tmp_path / "sessions"
    S.main(["--dry-run", "--blocks", str(bf), "--out-dir", str(out), "--seed", str(seed), *extra])
    (d,) = list(out.iterdir())
    return d, json.loads((d / "session.json").read_text(encoding="utf-8"))


def test_prompter_dry_run_writes_session_and_two_wavs(tmp_path, capsys):
    d, s = dry_session(tmp_path, ["--speaker", "spk02", "--device", "c3-b", "--tag", "volume=loud"])
    assert s["status"] == "complete" and s["dry_run"]
    assert s["device"] == "c3-b" and s["tv_source"] == "tv-c3b" and s["speaker"] == "spk02"
    assert s["tags"] == {"volume": "loud"} and s["split"] == "unassigned"
    assert set(s["sources"]) == {"pc-mic", "webcam-mic"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", s["t0_utc"])
    kinds = [b["kind"] for b in s["blocks"]]
    assert kinds == ["kivi", "near", "silence"]
    assert [len(b["cues"]) for b in s["blocks"]] == [4, 4, 0]
    labels = {c["label"] for c in s["blocks"][1]["cues"]}
    assert labels == {"near:kiwi", "near:aquí vi"}
    for b in s["blocks"][:2]:  # cue spacing 5 s +- 1 s
        offs = [c["offset_s"] for c in b["cues"]]
        gaps = np.diff(offs)
        assert ((gaps > 3.9) & (gaps < 6.2)).all(), gaps
        assert all(re.search(r"Z$", c["utc"]) for c in b["cues"])
    for name in ("pc-mic.wav", "webcam-mic.wav"):
        pcm = C.read_pcm16(d / name)
        first = s["sources"][name[:-4]]["first_sample_offset_s"]
        # the dry run compresses time 100x, so thread wake-up jitter of 10 ms is 1 session second
        assert -0.5 < len(pcm) / RATE - (s["end_offset_s"] - first) < 2.5
        assert stat.S_IMODE((d / name).stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "say KIVI now" in out and "\a" not in out  # text only, no terminal bell


def test_prompter_refuses_names_and_bad_blocks(tmp_path):
    with pytest.raises(SystemExit):
        S.main(["--dry-run", "--speaker", "Maria", "--out-dir", str(tmp_path)])
    with pytest.raises(SystemExit):
        S.validate_blocks([{"name": "x", "kind": "kivi", "n": 3, "interval_s": 3, "jitter_s": 1}])
    assert S.expand_blocks(S.default_blocks(), repeat=2)[-1]["name"].endswith("-r2")
    assert len(S.default_blocks()) == 6


def test_prompter_recorder_logic_with_a_fake_parec(tmp_path, monkeypatch):
    """The real recorder (ParecSource) against a stand-in `parec` script: no microphone is opened."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "parec"
    fake.write_text("#!/bin/sh\nexec python3 -c \"import sys,time\n"
                    "z=b'\\x00\\x01'*1600\n"
                    "while True:\n sys.stdout.buffer.write(z); sys.stdout.buffer.flush(); time.sleep(0.1)\"\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    clock = S.Clock(1.0)
    src = S.ParecSource("pc-mic", "dummy-source", tmp_path / "x.wav", clock)
    src.start()
    assert src.wait_first(5)
    import time
    time.sleep(0.8)
    src.finish()
    info = src.info()
    assert info["samples"] > 8000 and info["first_sample_offset_s"] is not None and not info["error"]
    assert len(C.read_pcm16(tmp_path / "x.wav")) == info["samples"]


# ---------------------------------------------------------------------------------------------
# extraction end to end (local fake of /voice-recordings)
# ---------------------------------------------------------------------------------------------





# ---------------------------------------------------------------------------------------------
# scorer
# ---------------------------------------------------------------------------------------------


def fake_session(cues_by_block):
    """A session record with the given blocks: [(name, kind, [(offset, label, word)], start, end)]."""
    blocks = []
    for i, (name, kind, cues, start, end) in enumerate(cues_by_block, 1):
        blocks.append({"index": i, "name": name, "kind": kind, "conditions": {"position_m": 1.5, "tv": "off"},
                       "start_offset_s": start, "end_offset_s": end,
                       "cues": [{"index": j, "offset_s": o, "label": lab, "word": w} for j, (o, lab, w) in enumerate(cues, 1)]})
    return {"session_id": "t", "device": "c3-a", "speaker": "spk01", "split": "dev",
            "sources": {"pc-mic": {"first_sample_offset_s": 0.0}}, "blocks": blocks}





needs_work = pytest.mark.skipif(not (WORK / "final/kivi_uncalibrated.tflite").exists(), reason="no kivi work directory")








# ---------------------------------------------------------------------------------------------
# split policy, energy segmentation, phone import, button takes, negatives
# ---------------------------------------------------------------------------------------------


def bursts_audio(seconds, bursts, seed=0, floor=30.0, amp=6000.0):
    """A quiet noise floor with 0.5 s band-limited noise bursts starting at `bursts` (s)."""
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(int(seconds * RATE)) * floor
    for t in bursts:
        n = int(0.5 * RATE)
        b = rng.standard_normal(n)
        sp = np.fft.rfft(b)
        f = np.fft.rfftfreq(n, 1 / RATE)
        sp[(f < 200) | (f > 3000)] = 0
        b = np.fft.irfft(sp, n)
        b = b / np.abs(b).max() * amp * np.hanning(n) ** 0.3
        i = int(t * RATE)
        x[i:i + n] += b
    return np.clip(x, -32000, 32000).astype(np.int16)


def test_split_policy():
    assert C.channel_split("unassigned", "pc-mic") == "train-only"
    assert C.channel_split("train", "webcam-mic") == "train-only"
    assert C.channel_split("test", "pc-mic") == "test"      # never leaks into training
    assert C.channel_split("dev", "tv-c3b") == "dev" and C.channel_split("train", "tv-c3a") == "train"
    assert C.channel_split("unassigned", "phone") == "train-only"
    assert not C.is_eval_channel("phone") and C.is_eval_channel("tv-c3b")
    assert set(C.SPLITS) == {"unassigned", "train", "dev", "test", "train-only"}
    with pytest.raises(KeyError):
        C.make_row(nonsense=1)


def test_energy_segments_find_bursts():
    x = bursts_audio(30, [3.0, 8.0, 8.4, 20.0])
    segs = C.energy_segments(x)
    starts = [round(s["t0"]) for s in segs]
    assert starts == [3, 8, 20], segs    # 8.0 and 8.4 merge into one take of about 0.9 s
    assert all(s["snr_db"] > 20 for s in segs)
    assert C.energy_segments(np.zeros(RATE * 5, np.int16)) == []


def test_phone_import(tmp_path, capsys):
    import subprocess
    bursts = [2 + 3 * i for i in range(6)]
    memo = tmp_path / "Maria memo.wav"            # a name-like file name must not be kept
    C.write_wav_new(memo, bursts_audio(25, bursts, seed=4))
    flac = tmp_path / "memo48.flac"               # decode + resample path (48 kHz stereo input)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(memo), "-ar", "48000", "-ac", "2", str(flac)], check=True)
    ds = tmp_path / "ds"
    base = [str(flac), "--speaker", "spk02", "--dataset-root", str(ds)]
    with pytest.raises(SystemExit):               # the split is required, there is no default
        P.main(base + ["--consent-confirmed"])
    with pytest.raises(SystemExit):               # consent is required
        P.main(base + ["--split", "train"])
    with pytest.raises(SystemExit):               # names are refused
        P.main([str(flac), "--speaker", "Maria", "--split", "train", "--consent-confirmed", "--dataset-root", str(ds)])
    P.main(base + ["--split", "train", "--dry-run"])
    assert not ds.exists()
    assert P.main(base + ["--split", "train", "--consent-confirmed", "--expect", "7"]) is None   # mismatch: nothing written
    assert not ds.exists()
    P.main(base + ["--split", "train", "--consent-confirmed", "--expect", "6", "--near-labels", "kiwi,Tivi"])
    rows = C.read_jsonl(ds / "manifest.jsonl")
    assert [r["label"] for r in rows] == ["kivi"] * 4 + ["near:kiwi", "near:tivi"]
    assert {r["source"] for r in rows} == {"phone"} and {r["speaker"] for r in rows} == {"spk02"}
    assert {r["split"] for r in rows} == {"train"} and all(r["consent_confirmed"] for r in rows)
    assert all(not r["eval_channel"] for r in rows)
    sid = rows[0]["session"]
    names = sorted(p.name for p in (ds / sid / "phone" / "raw").iterdir())
    assert names == ["memo-16k.wav", "original.flac"]
    assert len(C.read_pcm16(ds / sid / "phone" / "raw" / "memo-16k.wav")) / RATE == pytest.approx(25, abs=0.1)
    for r in rows:
        n = len(C.read_pcm16(ds / r["clip"])) / RATE
        assert 1.0 < n <= 3.0
    assert stat.S_IMODE((ds / sid).stat().st_mode) == 0o700
    assert "Maria" not in json.dumps(rows) + (ds / sid / "session.json").read_text()


def make_wake_tree(root, prefix, run, start, x, pipeline="Kiwi"):
    d = root / (prefix + "0000aaaa") / pipeline / str(run)
    d.mkdir(parents=True)
    f = d / "00_wake-wake_word.openwakeword.wav"
    hdr = C.wav_header(0)
    f.write_bytes(hdr[:4] + b"\xff\xff\xff\xff" + hdr[8:40] + b"\xff\xff\xff\xff" + x.astype("<i2").tobytes())
    end = start + len(x) / RATE
    os.utime(f, (end, end))
    return f


T0 = 1_800_000_000.0





def test_negatives_selection_and_copy(tmp_path):
    # ramp audio so the copied pieces can be checked sample by sample
    def ramp(sec):
        return (np.arange(int(sec * RATE)) % 30000).astype(np.int16)
    xa, xb, xc = ramp(200), ramp(200), ramp(300)
    make_wake_tree(tmp_path / "rec", "bd8256", 1, T0, xa)
    make_wake_tree(tmp_path / "rec", "bd8256", 2, T0 + 230, xb)      # follows run 1 within 60 s
    make_wake_tree(tmp_path / "rec", "fda18f", 3, T0, xc)
    live = make_wake_tree(tmp_path / "rec", "fda18f", 4, T0 + 400, ramp(60))
    now = T0 + 430 + 20                                           # file 4 was written 20 s ago: not closed
    pf = tmp_path / "presses.json"
    pf.write_text(json.dumps([X.utc_str(T0 + 100)]))
    ds = tmp_path / "ds"
    # a recorded session whose cue falls into run 3
    sdir = ds / "sess1"
    sdir.mkdir(parents=True)
    (sdir / "session.json").write_text(json.dumps({"t0_utc": X.utc_str(T0 + 140), "blocks": [
        {"cues": [{"offset_s": 50.0}]}]}))
    import argparse
    ns = argparse.Namespace(dataset_root=str(ds), hours=5.0, devices=None, devices_file=None, context="x", namespace="x",
                            pod="x", container=None, recordings_root="/x", presses_file=str(pf), no_presses=False,
                            pg_pod="x", pg_container="x", pg_user="x", pg_db="x", min_age_s=600.0, trim_start_s=0.3,
                            boundary_s=7.0, followup_s=60.0, exclude_s=10.0, min_seg_s=10.0, seed=1,
                            now=now + 700, dry_run=False)
    # plan of file 1: [0.3, 193] minus [90, 110]; file 2 starts 7 s later because of the follow-up rule
    f1 = {"start": T0, "mtime": T0 + 200}
    marks = N.known_marks(ds, [T0 + 100])
    assert T0 + 190 in marks                                       # the session cue (t0 + 140 + 50)
    assert N.plan_file(f1, None, [T0 + 100], ns) == [(T0 + 0.3, T0 + 90), (T0 + 110, T0 + 193)]
    f2 = {"start": T0 + 230, "mtime": T0 + 430}
    assert N.plan_file(f2, T0 + 200, [], ns) == [(T0 + 237, T0 + 423)]
    assert N.plan_file(f2, None, [], ns) == [(T0 + 230.3, T0 + 423)]
    assert N.plan_file({"start": T0, "mtime": T0 + 12}, None, [], ns) == []      # too short after trimming
    summary = N.run(ns, X.LocalFetcher(tmp_path / "rec"))
    assert summary["c3-a"]["files_picked"] == 2 and summary["c3-b"]["files_picked"] == 2   # file 4 is closed at now + 700
    rows = C.read_jsonl(ds / "negatives" / "manifest.jsonl")
    assert {r["source"] for r in rows} == {"tv-c3a", "tv-c3b"} and {r["label"] for r in rows} == {"negative"}
    r1 = [r for r in rows if r["source_run"] == "Kiwi/1"]
    assert len(r1) == 2
    got = C.read_pcm16(ds / r1[1]["clip"])
    assert len(got) == 70 * RATE   # 110 s .. 180 s: the cue at 190 s cuts 10 s before it and (got == xa[110 * RATE:110 * RATE + len(got)]).all()
    # nothing around the press (T0+100 +-10 s) or the session cue (T0+190 +-10 s) was copied
    for r in rows:
        if r["source_run"] in ("Kiwi/1", "Kiwi/3"):
            t0, t1 = X.parse_utc(r["start_utc"]).timestamp(), X.parse_utc(r["end_utc"]).timestamp()
            assert not (t0 < T0 + 110 and t1 > T0 + 90)
    n_before = len(rows)
    N.run(ns, X.LocalFetcher(tmp_path / "rec"))                    # re-run: nothing new, nothing overwritten
    assert len(C.read_jsonl(ds / "negatives" / "manifest.jsonl")) == n_before
    assert stat.S_IMODE((ds / "negatives" / "c3-a").stat().st_mode) == 0o700


def test_noncue_segments_with_zero_min_len_terminates():
    """Regression: min_len=0 once looped forever (and grew to about 48 GB). Run in a subprocess with a
    time and memory limit so that a regression fails instead of eating the machine."""
    import subprocess
    code = ("import kivi_real_common as C, test_kivi_real as T, resource; "
            "resource.setrlimit(resource.RLIMIT_AS, (4 << 30, 4 << 30)); "
            "s = T.fake_session([('k', 'kivi', [(5.0, 'kivi', 'kivi')], 0.0, 30.0)]); "
            "w = C.cue_windows(s); "
            "print(len(C.noncue_segments(s, w, min_len=0.0, max_len=1e9)))")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(HERE), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == "2", r.stderr[-300:]


# ---------------------------------------------------------------------------------------------
# review fixes: ranges, part files, run ends, unaligned files, crash safety, pre-flight, splits
# ---------------------------------------------------------------------------------------------


def test_static_no_deletion_and_one_rename():
    banned = re.compile(r"os\.(remove|unlink|rmdir|replace)|shutil\.(rmtree|move)|\.unlink\(|rmtree|os\.truncate|\.truncate\(")
    for name in ("kivi_extract.py", "kivi_real_common.py", "kivi_score.py", "kivi_import_phone.py", "kivi_negatives.py", "kivi_dataset_marks.py"):
        text = (HERE / name).read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert not banned.search(code), name
        n_rename = len(re.findall(r"os\.rename\(", code))
        assert n_rename == (1 if name == "kivi_real_common.py" else 0), (name, n_rename)   # finalize_part only


def prepared_session(tmp_path, blocks=SMALL_BLOCKS, seed=3):
    d, s = dry_session(tmp_path, blocks=blocks, seed=seed)
    ref = speechlike(len(C.read_pcm16(d / "pc-mic.wav")) / RATE, 21)
    rng = np.random.default_rng(5)
    web = delayed(ref, 0.0123, rng)[:len(ref)]
    for name, x in (("pc-mic.wav", ref), ("webcam-mic.wav", web)):
        (d / name).unlink()
        C.write_wav_new(d / name, x)
    return d, s, ref, rng, C.session_pc_offset(s), X.parse_utc(s["t0_utc"]).timestamp()


def put_tv_file(tmp_path, s, t0, sig, session_start_s, err=0.7, run=123456789, prefix="bd8256"):
    """A wake file whose first sample is at session time `session_start_s`; its listed mtime is `err` s off."""
    start_epoch = t0 + session_start_s
    return make_wake_tree(tmp_path / "rec", prefix + "abcdef", run, start_epoch + err, sig), start_epoch + err + len(sig) / RATE


def extract_args(tmp_path, d, ds, *extra):
    return ["--session-dir", str(d), "--dataset-root", str(ds), "--from-dir", str(tmp_path / "rec"), "--win-s", "10", *extra]


def test_extract_copies_only_the_window_and_aligns(tmp_path):
    d, s, ref, rng, o_pc, t0 = prepared_session(tmp_path)
    d_true, lead, tail = 1.84, 150.0, 100.0
    sig = np.concatenate([speechlike(lead, 41), delayed(ref, d_true, rng, gain=10, snr_db=8), speechlike(tail, 42)])
    f, mtime = put_tv_file(tmp_path, s, t0, sig, o_pc - d_true - lead)
    other = tmp_path / "rec" / "fda18fffff" / "Kiwi" / "999"                     # another satellite: ignored
    other.mkdir(parents=True)
    (other / "00_wake-x.wav").write_bytes(C.wav_header(0) + b"\x00\x00" * 1000)
    ds = tmp_path / "datasets" / "real"
    args = extract_args(tmp_path, d, ds, "--now", str(mtime + 1000))
    X.main(args + ["--dry-run"])
    assert not ds.exists()
    X.main(args)
    sid = s["session_id"]
    rows = C.read_jsonl(ds / "manifest.jsonl")
    assert {r["source"] for r in rows} == {"tv-c3a", "pc-mic", "webcam-mic"}
    assert all(set(r) == set(C.MANIFEST_FIELDS) for r in rows)
    n_cues = sum(len(b["cues"]) for b in s["blocks"])
    for src in ("tv-c3a", "pc-mic", "webcam-mic"):
        cues = [r for r in rows if r["source"] == src and r["kind"] == "cue"]
        assert len(cues) == n_cues == 8
        assert {r["label"] for r in cues} == {"kivi", "near:kiwi", "near:aquí vi"}
    assert "silence" in {r["label"] for r in rows}
    # only the session window +-60 s was copied, not the 250 s file; the copy is a proper WAV
    raw = list((ds / sid / "tv-c3a" / "raw").glob("*.wav"))
    assert len(raw) == 1 and "@" in raw[0].name
    n = len(C.read_pcm16(raw[0])) / RATE
    ref_dur = len(ref) / RATE
    assert ref_dur + 100 < n < ref_dur + 125 and n < len(sig) / RATE - 100, n
    assert not list((ds / sid / "tv-c3a" / "raw").glob("*.part"))
    al = json.loads((ds / sid / "tv-c3a" / "alignment.json").read_text())
    rec = al["files"][0]
    assert rec["ok"] and rec["included"] and rec["run_end_ref_s"] is None
    expect_a = d_true + lead - rec["copied_from_s"]                      # offset relative to the copy's sample 0
    assert abs(rec["offset_a_s"] - expect_a) < 0.003, (rec["offset_a_s"], expect_a)
    r0 = next(r for r in rows if r["source"] == "tv-c3a" and r["kind"] == "cue")
    assert r0["device"] == "c3-a" and r0["session_device"] == "c3-a" and r0["split"] == "unassigned"
    assert r0["source_run"] == "Kiwi/123456789" and r0["align_ok"] and r0["eval_channel"]
    assert {r["split"] for r in rows if r["source"] == "pc-mic"} == {"train-only"}
    assert {r["eval_channel"] for r in rows if r["source"] != "tv-c3a"} == {False}
    # the satellite clip and the desk clip hold the same content at the same time
    tvc = next(r for r in rows if r["source"] == "tv-c3a" and r["label"] == "kivi")
    pcc = next(r for r in rows if r["source"] == "pc-mic" and r["clip"].split("/", 2)[2] == tvc["clip"].split("/", 2)[2])
    a_, b_ = C.read_pcm16(ds / tvc["clip"]), C.read_pcm16(ds / pcc["clip"])
    lag, conf = C.gcc_phat(b_, np.concatenate([np.zeros(800, np.int16), a_, np.zeros(800, np.int16)]), 800)
    assert len(a_) == len(b_) and abs(lag) < 0.002 and conf > 8, (lag, conf)
    for p in [ds, ds / sid, ds / sid / "tv-c3a", ds / sid / "tv-c3a" / "clips"]:
        assert stat.S_IMODE(p.stat().st_mode) == 0o700, p
    for p in ds.rglob("*"):
        assert stat.S_IMODE(p.stat().st_mode) in (0o700, 0o600), p
    # request-audio files are fetched too when they exist
    assert (ds / sid / "tv-c3a" / "raw-stt").is_dir()
    rep = json.loads(next(ds.glob(f"{sid}/extract-*.json")).read_text())
    ps = {k: v for k, v in rep["per_source"]["tv-c3a"].items() if k != "cue_coverage"}
    assert ps == {"cues": 8, "cues_cut": 8, "lost_cues": 0, "run_end_in_window": 0, "segments_uncovered": 0,
                  "rows_reconciled": 0, "coverage": {"full": 8, "partial": 0, "none": 0}}


def test_extract_run_end_inside_a_cue_window_is_a_detection_not_uncovered(tmp_path, capsys):
    d, s, ref, rng, o_pc, t0 = prepared_session(tmp_path)
    cue = s["blocks"][0]["cues"][-1]["offset_s"]                  # the last kivi cue of block 1
    tau_end = cue - o_pc + 1.5                                     # the server closes the file 1.5 s after the cue
    d_true = 1.2
    sig = delayed(ref[:int(tau_end * RATE)], d_true, rng, gain=10, snr_db=8)
    f, mtime = put_tv_file(tmp_path, s, t0, sig, o_pc - d_true, err=0.4)
    ds = tmp_path / "ds"
    X.main(extract_args(tmp_path, d, ds, "--win-s", "5", "--now", str(mtime + 1000)))
    out = capsys.readouterr().out
    rep = json.loads(next(ds.glob("*/extract-*.json")).read_text())
    st = rep["per_source"]["tv-c3a"]
    assert (st["cues"], st["run_end_in_window"], st["cues_cut"], st["lost_cues"]) == (8, 1, 4, 4)
    rows = [r for r in C.read_jsonl(ds / "manifest.jsonl") if r["source"] == "tv-c3a" and r["kind"] == "cue"]
    flagged = [r for r in rows if r["run_end_in_window"]]
    assert len(flagged) == 1 and flagged[0]["cue_index"] == 4 and flagged[0]["block"] == 1
    assert 1.8 < len(C.read_pcm16(ds / flagged[0]["clip"])) / RATE < 2.2          # cue - 0.5 s .. file end
    assert "lost or partly uncovered 4" in out and "server run end inside the window 1" in out
    al = json.loads((ds / s["session_id"] / "tv-c3a" / "alignment.json").read_text())
    assert al["files"][0]["run_end_ref_s"] == pytest.approx(tau_end, abs=0.01)


def test_unaligned_files_are_left_out_unless_asked(tmp_path, capsys):
    d, s, ref, rng, o_pc, t0 = prepared_session(tmp_path)
    sig = speechlike(len(ref) / RATE + 20, 77)                      # unrelated audio: cannot align
    f, mtime = put_tv_file(tmp_path, s, t0, sig, o_pc - 5.0)
    ds1, ds2 = tmp_path / "ds1", tmp_path / "ds2"
    X.main(extract_args(tmp_path, d, ds1, "--now", str(mtime + 1000)))
    out = capsys.readouterr().out
    assert "NOT TRUSTED" in out and "LEFT OUT" in out
    assert not [r for r in C.read_jsonl(ds1 / "manifest.jsonl") if r["source"] == "tv-c3a"]
    assert [r for r in C.read_jsonl(ds1 / "manifest.jsonl") if r["source"] == "pc-mic"]
    rec = json.loads((ds1 / s["session_id"] / "tv-c3a" / "alignment.json").read_text())["files"][0]
    assert rec["ok"] is False and rec["included"] is False
    X.main(extract_args(tmp_path, d, ds2, "--include-unaligned", "--now", str(mtime + 1000)))
    tv = [r for r in C.read_jsonl(ds2 / "manifest.jsonl") if r["source"] == "tv-c3a"]
    assert tv and {r["align_ok"] for r in tv} == {False}


def test_coarse_alignment_tries_every_window():
    """The recorder that hears a loud TV in half of the windows must not stop the alignment: windows
    are ranked by the weaker of the two signals and all of them are tried."""
    rng = np.random.default_rng(8)
    ref = speechlike(120, 51)
    sig = delayed(ref, 2.5, rng)
    noisy = ref.astype(np.float32).copy()
    for k in range(0, 120, 10):                                     # loud interference in the desk mic only
        a = int(k * RATE)
        noisy[a:a + 5 * RATE] += np.random.default_rng(k).standard_normal(5 * RATE) * 20000
    noisy = np.clip(noisy, -32000, 32000).astype(np.int16)
    al = C.align_stream(noisy, sig, 2.5 + 1.2, win_s=5.0)
    assert al["n_coarse"] == al["n_windows"] > 20                   # every window, not the 10 loudest
    assert al["ok"] and abs(al["a"] - 2.5) < 0.003, al["a"]


def test_manifest_is_crash_safe_and_reconciled(tmp_path, capsys):
    d, s, ref, rng, o_pc, t0 = prepared_session(tmp_path)
    ds = tmp_path / "ds"
    args = extract_args(tmp_path, d, ds, "--no-tv")
    X.main(args)
    man = ds / "manifest.jsonl"
    lines = man.read_text().splitlines()
    assert len(lines) > 10
    man.write_text("\n".join(lines[:-3]) + "\n" + '{"session": "trunc')          # a crash: 3 rows lost, a half line
    capsys.readouterr()
    assert len(C.read_jsonl(man)) == len(lines) - 3                                # the half line is skipped, with a warning
    assert "not valid JSON" in capsys.readouterr().err
    X.main(args)                                                                   # re-run: clips exist, rows are reconciled
    rows = C.read_jsonl(man)
    assert len(rows) == len(lines)
    assert sorted(r["clip"] for r in rows) == sorted(json.loads(l)["clip"] for l in lines)
    rep = sorted(ds.glob(f"{s['session_id']}/extract-*.json"))[-1]
    assert sum(v["rows_reconciled"] for v in json.loads(rep.read_text())["per_source"].values()) == 3
    raw = man.read_bytes()
    assert raw.endswith(b"\n") and b'{"session": "trunc\n' in raw                  # repaired by a newline, nothing deleted


def test_short_copy_fails_and_leaves_only_a_part_file(tmp_path):
    make_wake_tree(tmp_path / "rec", "bd8256", 5, T0, bursts_audio(30, [], seed=1))
    (f,) = X.list_files(X.LocalFetcher(tmp_path / "rec"), "bd8256", "wake")

    class Short(X.LocalFetcher):
        def fetch_range(self, path, lo, hi):
            for b in super().fetch_range(path, lo, hi - 2):
                yield b

    rawdir = tmp_path / "raw"
    rawdir.mkdir()
    with pytest.raises(SystemExit):
        X.fetch_samples(Short(tmp_path / "rec"), f, 0, f["n"], rawdir)
    assert not list(rawdir.glob("*.wav")) and len(list(rawdir.glob("*.part"))) == 1
    dst, name = X.fetch_samples(X.LocalFetcher(tmp_path / "rec"), f, 16000, 16000 * 10, rawdir)   # a good copy of a range
    assert len(C.read_pcm16(dst)) == 16000 * 9 and name.endswith("@16000-160000.wav")
    with pytest.raises(FileExistsError):
        C.write_wav_new(dst, np.zeros(10, np.int16))


def test_kubectl_fetcher_byte_ranges_with_a_stand_in_kubectl(tmp_path, monkeypatch):
    """KubectlFetcher against a `kubectl` that runs the command after `--` locally: the glob and stat
    listing and the dd byte range (not whole files)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    k = bindir / "kubectl"
    k.write_text('#!/bin/sh\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n')
    k.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    x = (np.arange(20000) % 30000).astype(np.int16)
    f = make_wake_tree(tmp_path / "rec", "bd8256", 7, T0, x)
    kf = X.KubectlFetcher("ctx", "ns", "pod", None, root=str(tmp_path / "rec"))
    (item,) = kf.list_run_files("bd8256", "wake")
    assert item["size"] == 44 + 40000
    data = f.read_bytes()
    for lo, hi in ((44, 44 + 40000), (5013, 17001), (4096, 8192), (12345, 12346)):
        assert b"".join(kf.fetch_range(item["path"], lo, hi)) == data[lo:hi], (lo, hi)
    with pytest.raises(SystemExit):
        list(kf.fetch_range("/x; rm -rf /", 0, 10))                  # paths are validated


def test_pipeline_preflight_and_warning(tmp_path, capsys):
    kivi = json.dumps({"data": {"items": [{"id": "a", "name": "P1", "wake_word_id": "kivi"},
                                          {"id": "b", "name": "P2", "wake_word_id": "alexa_v0.1"}], "preferred_item": "a"}})
    alexa = kivi.replace('"preferred_item": "a"', '"preferred_item": "b"')
    r = C.pipeline_preflight("c", "n", "p", text=kivi)
    assert r["preferred_wake_word"] == "kivi" and r["live"] and r["others"] == ["alexa_v0.1"]
    r = C.pipeline_preflight("c", "n", "p", text=alexa)
    assert r["preferred_wake_word"] == "alexa_v0.1" and not r["live"] and r["any_live"]   # kivi is on another pipeline
    assert "error" in C.pipeline_preflight("c", "n", "p", text="not json")
    d, s, ref, rng, o_pc, t0 = prepared_session(tmp_path)
    pf = tmp_path / "pipes.json"
    pf.write_text(kivi)
    X.main(extract_args(tmp_path, d, tmp_path / "ds", "--no-tv", "--pipelines-file", str(pf), "--dry-run"))
    X.main(extract_args(tmp_path, d, tmp_path / "ds", "--pipelines-file", str(pf), "--dry-run"))
    assert "'kivi' is a live wake word" in capsys.readouterr().out


def test_button_takes_press_then_say_flags_review_and_ignores_restored_states(tmp_path):
    x = bursts_audio(120, [22.0, 53.5, 91.0, 94.0], seed=7)
    make_wake_tree(tmp_path / "rec", "fda18f", 111, T0, x)
    presses = [T0 + 20.0, T0 + 50.0, T0 + 80.0, T0 + 90.0, T0 + 500.0]
    pf = tmp_path / "presses.json"
    pf.write_text(json.dumps([X.utc_str(t) for t in presses]))
    ds = tmp_path / "ds"
    base = ["--button-takes", "--device", "c3-b", "--speaker", "spk02", "--since", X.utc_str(T0 - 60),
            "--until", X.utc_str(T0 + 600), "--presses-file", str(pf), "--dataset-root", str(ds),
            "--from-dir", str(tmp_path / "rec"), "--button-order", "press-then-say"]
    with pytest.raises(SystemExit):
        X.main(base)                                            # --split is required
    X.main(base + ["--split", "test"])
    rows = C.read_jsonl(ds / "manifest.jsonl")
    assert {r["revision"] for r in rows} == {""} and {r["tags"]["button_order"] for r in rows} == {"press-then-say"}
    assert [r["cue_index"] for r in rows] == [1, 2, 4]
    assert {r["source"] for r in rows} == {"tv-c3b"} and {r["speaker"] for r in rows} == {"spk02"}
    assert [r["needs_review"] for r in rows] == [False, False, True]
    assert [r["split"] for r in rows] == ["test", "test", "unassigned"]       # a review take is kept out of test
    assert rows[2]["split_requested"] == "test" and rows[2]["n_candidates"] == 2
    assert 1.5 < rows[0]["onset_after_press_s"] < 2.5 and 3.0 < rows[1]["onset_after_press_s"] < 4.0
    rep = json.loads(next(ds.glob("button-*/extract-*.json")).read_text())
    assert (rep["presses"], rep["clips"], rep["no_clear_onset"], rep["no_file"], rep["needs_review"]) == (5, 3, 1, 1, 1)
    X.main(base + ["--split", "test"])                                         # idempotent
    assert len(C.read_jsonl(ds / "manifest.jsonl")) == 3
    ds2 = tmp_path / "ds2"
    X.main([str(ds2) if t == str(ds) else t for t in base] + ["--split", "test", "--keep-review-in-split"])
    assert [r["split"] for r in C.read_jsonl(ds2 / "manifest.jsonl")] == ["test", "test", "test"]
    # a state restored after a Home Assistant restart repeats the old press time: one press, not two
    st = [X.utc_str(T0 + 20).replace("Z", "+00:00"), X.utc_str(T0 + 20).replace("Z", "+00:00"), "unknown", "",
          X.utc_str(T0 + 50)]
    assert X.state_press_times(st, T0, T0 + 100) == [T0 + 20, T0 + 50]
    assert X.state_press_times(st, T0 + 30, T0 + 100) == [T0 + 50]


def button_args(tmp_path, presses, *extra, ds="ds"):
    pf = tmp_path / "presses.json"
    pf.write_text(json.dumps([X.utc_str(t) for t in presses]))
    return ["--button-takes", "--device", "c3-b", "--speaker", "spk02", "--since", X.utc_str(T0 - 60),
            "--until", X.utc_str(T0 + 600), "--presses-file", str(pf), "--dataset-root", str(tmp_path / ds),
            "--from-dir", str(tmp_path / "rec"), "--split", "test", *extra]


def test_button_takes_default_is_say_then_press_and_window(tmp_path):
    """The word is spoken before the press: window max(press - 4 s, previous press + 0.3 s) ... press + 1 s."""
    words = [10.0, 13.0, 20.0, 25.0, 50.0, 52.0, 80.0]
    x = bursts_audio(120, words, seed=11)
    make_wake_tree(tmp_path / "rec", "fda18f", 111, T0, x)
    presses = [T0 + t for t in (11.5, 14.6, 21.5, 22.3, 26.5, 40.0, 50.8, 53.2, 90.0)]
    # 11.5 and 14.6: two ordinary takes; 21.5 + 22.3 is a double press of the word at 20.0 (0.8 s apart);
    # 26.5: the word at 25.0; 40.0: no speech in 36..41; 50.8 / 53.2: word A at 50.0 and word B at 52.0, so B's
    # window (53.2 - 4 = 49.2) would hold A too, but it starts at A's press + 0.3 = 51.1 and holds only B; 90.0: the word at 80.0 is 10 s before
    X.main(button_args(tmp_path, presses))                                       # no --button-order: the default
    rows = C.read_jsonl(tmp_path / "ds" / "manifest.jsonl")
    assert {r["revision"] for r in rows} == {X.SAY_THEN_PRESS_REVISION} and all(f"/clips-{X.SAY_THEN_PRESS_REVISION}/00-button-takes/" in r["clip"] for r in rows)
    assert {r["tags"]["button_order"] for r in rows} == {"say-then-press"}
    assert [r["cue_index"] for r in rows] == [1, 2, 3, 4, 6, 7] and 5 not in [r["cue_index"] for r in rows]
    assert [r["tags"]["n_presses"] for r in rows] == [1, 1, 2, 1, 1, 1]
    assert [r["needs_review"] for r in rows] == [False] * 6                        # B's window excludes A's burst
    assert [r["split"] for r in rows] == ["test"] * 6
    # onsets are before the first press of the take (negative), inside 4 s before it
    assert all(-4.0 <= r["onset_after_press_s"] <= -0.5 for r in rows)
    assert abs(rows[0]["onset_after_press_s"] + 1.5) < 0.2 and abs(rows[2]["onset_after_press_s"] + 1.5) < 0.2
    assert abs(rows[4]["onset_after_press_s"] + 0.8) < 0.2 and abs(rows[5]["onset_after_press_s"] + 1.2) < 0.2
    rep = json.loads(next((tmp_path / "ds").glob("button-*/extract-*.json")).read_text())
    assert (rep["presses"], rep["takes"], rep["double_presses"], rep["clips"], rep["no_clear_onset"],
            rep["needs_review"], rep["button_order"], rep["revision"]) == (9, 8, 1, 6, 2, 0, "say-then-press", X.SAY_THEN_PRESS_REVISION)
    X.main(button_args(tmp_path, presses))                                       # idempotent
    assert len(C.read_jsonl(tmp_path / "ds" / "manifest.jsonl")) == 6


def test_button_takes_double_press_merge_and_say_then_press_flags_two_words(tmp_path):
    assert X.merge_presses([5.0, 5.8, 6.2, 9.0, 12.0, 13.4, 20.0], 1.5) == [
        (5.0, 6.2, 3), (9.0, 9.0, 1), (12.0, 13.4, 2), (20.0, 20.0, 1)]          # chained: < 1.5 s from the previous press
    assert X.merge_presses([5.0, 6.5], 1.5) == [(5.0, 5.0, 1), (6.5, 6.5, 1)]    # exactly 1.5 s apart: two takes
    a = Namespace(before_s=4.0, after_s=1.0, min_gap_after_prev_s=0.3, tolerance_s=1.0, window_s=6.0)
    assert X.take_window("say-then-press", 30.0, None, a) == (26.0, 31.0)
    assert X.take_window("say-then-press", 30.0, 28.0, a) == (28.3, 31.0)
    assert X.take_window("say-then-press", 30.0, 20.0, a) == (26.0, 31.0)
    assert X.take_window("press-then-say", 30.0, 28.0, a) == (29.0, 37.0)
    # two separate words in one window (no previous press to cut it): flagged and kept out of test
    x = bursts_audio(60, [20.0, 22.0], seed=5)
    make_wake_tree(tmp_path / "rec", "fda18f", 111, T0, x)
    X.main(button_args(tmp_path, [T0 + 23.0]))
    (row,) = C.read_jsonl(tmp_path / "ds" / "manifest.jsonl")
    assert row["needs_review"] and row["n_candidates"] == 2 and row["split"] == "unassigned"


def test_button_order_default_in_the_parser(tmp_path, capsys):
    x = bursts_audio(30, [10.0], seed=2)
    make_wake_tree(tmp_path / "rec", "fda18f", 111, T0, x)
    X.main(button_args(tmp_path, [T0 + 11.5], "--dry-run"))
    assert "order say-then-press" in capsys.readouterr().out
    X.main(button_args(tmp_path, [T0 + 11.5], "--dry-run", "--button-order", "press-then-say"))
    assert "order press-then-say" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        X.main(button_args(tmp_path, [T0 + 11.5], "--button-order", "bogus"))


def test_training_rows_and_ablation_and_drift():
    rows = [{"speaker": "spk01", "split": "train"}, {"speaker": "spk02", "split": "train"},       # phone memo of spk02
            {"speaker": "spk02", "split": "test"}, {"speaker": "spk01", "split": "train-only"},
            {"speaker": "spk01", "split": "unassigned"}, {"speaker": "spk01", "split": "train", "needs_review": True},
            {"speaker": "", "split": "train"}]
    prod = C.training_rows(rows)
    assert len(prod) == 4 and {"speaker": "spk02", "split": "test"} not in prod
    abl = C.training_rows(rows, exclude_speakers=["spk02"])
    assert all(r["speaker"] != "spk02" for r in abl) and len(abl) == 3
    assert C.sample_drift(16000 * 100, 100.0) == 0 and abs(C.sample_drift(16000 * 99, 100.0) + 0.01) < 1e-9
    assert abs(C.sample_drift(16000 * 100.4, 100.0)) < 0.005


def session_ns(tmp_path, **kw):
    d = dict(list_devices=False, print_protocol=False, device="c3-a", speaker="spk01", split="unassigned",
             desk_mic=None, webcam_mic=None, out_dir=str(tmp_path / "sessions"), blocks=str(tmp_path / "blocks.json"),
             only=None, repeat=1, tag=None, seed=3, context="c", namespace="n", pod="p", container=None,
             model_name="kivi", pipelines_file=None, no_preflight=True, no_wait=False, dry_run=False,
             fake_source="sine", time_scale=0.01)
    d.update(kw)
    return Namespace(**d)


def test_near_word_rows_of_the_target_phrase_are_negatives_for_it():
    """A session recorded FOR "oye kivi" carries its own confusers: `near:*` cues must become negatives
    for that phrase (they are what the model wakes on by mistake), while the session's positive takes
    never do. Takes of another phrase (the old "kivi" ones) keep working as before."""
    rows = [
        {"speaker": "spk01", "split": "train", "phrase": "oye kivi", "label": "oye kivi", "word": ""},
        {"speaker": "spk01", "split": "train", "phrase": "oye kivi", "label": "near:kiri", "word": "kiri"},
        {"speaker": "spk01", "split": "train", "phrase": "oye kivi", "label": "near:kivi", "word": "kivi"},
        {"speaker": "spk01", "split": "train", "phrase": "kivi", "label": "kivi", "word": ""},
        {"speaker": "spk01", "split": "test", "phrase": "oye kivi", "label": "near:kili", "word": "kili"},
        {"speaker": "spk01", "split": "train", "phrase": "oye kivi", "label": "near:oye kivi", "word": "oye kivi"},
    ]
    neg = C.negative_rows_for_phrase(rows, "oye kivi")
    labels = sorted(r["label"] for r in neg)
    assert labels == ["kivi", "near:kiri", "near:kivi"]        # own positives and test-split rows stay out
    assert all(r["negative_for"] == "oye kivi" and "label_original" in r for r in neg)
    # the same function for the old phrase: today's confuser rows of "oye kivi" are negatives for "kivi",
    # the "kivi" positives are not; and "near:kivi" is not either — its spoken word is that phrase itself
    neg_k = C.negative_rows_for_phrase(rows, "kivi")
    assert sorted(r["label"] for r in neg_k) == ["near:kiri", "near:oye kivi", "oye kivi"]


def test_v3_real_take_slot_names_the_target_phrase_take():
    """`takes` labels a cue by its label against the session's wake phrase, not by a hardcoded "kivi"."""
    import v3_real as R

    assert R.take_slot("oye kivi", "oye kivi") == "kivi"
    assert R.take_slot("Kivi", "kivi") == "kivi"
    assert R.take_slot("near:kiri", "oye kivi") == "near"
    assert R.take_slot("near:kivi", "kivi") == "near"          # a near-word take, not a positive


def test_session_json_is_written_at_start_and_updated(tmp_path):
    (tmp_path / "blocks.json").write_text(json.dumps(SMALL_BLOCKS))
    seen = []

    def prompt(msg):
        (j,) = (tmp_path / "sessions").glob("*/session.json")
        seen.append(json.loads(j.read_text()))

    S.run(session_ns(tmp_path), input_fn=prompt)
    assert len(seen) == 3
    first = seen[0]                                       # at the first "Enter when ready": the recording is running
    assert first["status"] == "recording" and first["blocks"] == []
    assert first["sources"]["pc-mic"]["first_sample_offset_s"] is not None
    assert len(seen[1]["blocks"]) == 1 and len(seen[1]["blocks"][0]["cues"]) == 4      # updated after block 1
    (j,) = (tmp_path / "sessions").glob("*/session.json")
    assert json.loads(j.read_text())["status"] == "complete"


def test_session_interrupt_keeps_files_and_marks_interrupted(tmp_path):
    (tmp_path / "blocks.json").write_text(json.dumps(SMALL_BLOCKS))
    calls = []

    def prompt(msg):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt

    S.run(session_ns(tmp_path), input_fn=prompt)
    (j,) = (tmp_path / "sessions").glob("*/session.json")
    s = json.loads(j.read_text())
    assert s["status"] == "interrupted" and len(s["blocks"]) == 1 and s["blocks"][0]["end_offset_s"] is not None
    assert s["sources"]["pc-mic"]["samples"] > 0 and (j.parent / "pc-mic.wav").stat().st_size > 44


def test_recorder_children_get_their_own_process_group(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "parec"
    fake.write_text("#!/bin/sh\nexec python3 -c \"import sys,time\n"
                    "z=b'\\x00\\x01'*1600\n"
                    "while True:\n sys.stdout.buffer.write(z); sys.stdout.buffer.flush(); time.sleep(0.1)\"\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    src = S.ParecSource("pc-mic", "dummy", tmp_path / "x.wav", S.Clock(1.0))
    src.start()
    try:
        assert src.wait_first(5)
        assert os.getpgid(src.proc.pid) != os.getpgid(os.getpid())      # Ctrl-C in the terminal does not reach it
    finally:
        src.finish()


def test_score_aggregation_lost_cues_and_run_ends():
    import kivi_score as K
    sess = fake_session([("k", "kivi", [(5.0, "kivi", "kivi"), (15.0, "kivi", "kivi")], 0.0, 30.0),
                         ("n", "near", [(35.0, "near:kiwi", "kiwi"), (45.0, "near:tivi", "tivi")], 30.0, 55.0),
                         ("s", "silence", [], 55.0, 65.0)])
    times = np.arange(0, 65, 0.08)
    pk = np.zeros(len(times))
    pk[np.argmin(abs(times - 6.0))] = 0.99     # cue 1 detected at 0.98
    pk[np.argmin(abs(times - 20.0))] = 0.975   # between cues: a false accept at 0.9 and 0.97
    pk[np.argmin(abs(times - 36.0))] = 0.96    # near-word kiwi
    pk[np.argmin(abs(times - 60.0))] = 0.91    # in silence
    pa = np.zeros(len(times))
    rep = K.analyze(sess, {"kivi": pk, "alexa": pa}, times, [[0.0, 65.0]], [0.5, 0.9, 0.97, 0.98, 0.99])
    kb = rep["blocks"][0]["cue_hits"]["kivi"]
    assert (kb["0.5"]["k"], kb["0.5"]["n"]) == (1, 2) and kb["0.98"]["k"] == 1 and kb["0.99"]["k"] == 0
    nb = rep["blocks"][1]["by_word"]
    assert nb["kiwi"]["kivi"]["0.9"]["k"] == 1 and nb["tivi"]["kivi"]["0.9"]["k"] == 0
    fa = rep["overall"]["false_accepts"]["kivi"]
    assert fa["0.9"]["k"] == 2 and fa["0.97"]["k"] == 1 and fa["0.99"]["k"] == 0, fa
    assert abs(fa["0.9"]["hours"] - (65 - 4 * 4.0) / 3600) < 1e-3
    # lost cues stay in the denominators as misses
    rep2 = K.analyze(sess, {"kivi": pk}, times, [[0.0, 10.0]], [0.5])
    b0 = rep2["blocks"][0]
    assert b0["lost_cues"] == 1 and b0["cue_hits"]["kivi"]["0.5"] == {**b0["cue_hits"]["kivi"]["0.5"], "k": 1, "n": 2}
    assert rep2["overall"]["lost_cues"] == 3 + 0 or rep2["overall"]["lost_cues"] >= 3
    # a closed file ending inside a cue window is a server detection: kept (clipped window), not lost
    rep3 = K.analyze(sess, {"kivi": pk}, times, [[0.0, 17.0]], [0.5, 0.9], run_ends=[17.0, 58.0])
    b = rep3["blocks"][0]
    assert b["run_end_cues"] == 1 and b["lost_cues"] == 0 and b["cue_hits"]["kivi"]["0.5"]["n"] == 2
    assert rep3["blocks"][2]["server_run_ends"] == 1 and rep3["overall"]["run_end_cues"] == 1
    md = K.to_markdown({**rep3, "session": "t", "source": "tv-c3a", "models": {"kivi": "k.tflite"}})
    assert "Coverage and server events" in md and "Near-word" in md and "EXCLUDED" in md


def synthetic_score_session(tmp_path, ok=True):
    import soundfile as sf
    mk = list(csv.DictReader(open(WORK / "eval_clips/eval_kivi.manifest.csv", encoding="utf-8")))
    nm = list(csv.DictReader(open(WORK / "eval_clips/eval_nearmiss.manifest.csv", encoding="utf-8")))
    kivi_clips = [r["file"] for r in mk if r["text"] == "kivi"][:12]
    near_clips = [(r["file"], r["text"]) for r in nm][:4]
    x = np.zeros(10 * RATE, np.int16)
    cues_k, cues_n = [], []
    for f in kivi_clips:
        c, _ = sf.read(WORK / "eval_clips/eval_kivi" / f, dtype="int16")
        cues_k.append((len(x) / RATE + 0.5, "kivi", "kivi"))
        x = np.concatenate([x, np.zeros(RATE // 2, np.int16), c, np.zeros(int(4.5 * RATE) - len(c), np.int16)])
    k_end = len(x) / RATE
    for f, t in near_clips:
        c, _ = sf.read(WORK / "eval_clips/eval_nearmiss" / f, dtype="int16")
        cues_n.append((len(x) / RATE + 0.5, f"near:{t}", t))
        x = np.concatenate([x, np.zeros(RATE // 2, np.int16), c, np.zeros(int(4.5 * RATE) - len(c), np.int16)])
    n_end = len(x) / RATE
    x = np.concatenate([x, np.zeros(15 * RATE, np.int16)])
    sess = fake_session([("kivi-b", "kivi", cues_k, 0.0, k_end), ("near", "near", cues_n, k_end, n_end),
                         ("silence", "silence", [], n_end, n_end + 15)])
    sdir = tmp_path / "ds" / "t"
    (sdir / "tv-c3a" / "raw").mkdir(parents=True)
    (sdir / "session.json").write_text(json.dumps(sess))
    C.write_wav_new(sdir / "tv-c3a" / "raw" / "x@0-1.wav", x)
    (sdir / "tv-c3a" / "alignment.json").write_text(json.dumps({"files": [
        {"name": "x@0-1.wav", "ok": ok, "included": True, "offset_a_s": 0.0, "b": 0.0, "median_conf": 30.0, "drift_ppm": 0.0,
         "coverage_s": [0, len(x) / RATE], "run_end_ref_s": None}]}))
    return sdir, len(kivi_clips)


@needs_work
def test_scorer_runs_the_server_runtime_on_raw_files(tmp_path):
    """kivi and alexa through pyopen-wakeword (the server's library) on held-out synthetic clips; an
    untrusted alignment leaves the file out (all cues lost, still in the denominators) unless asked."""
    import kivi_score as K
    sdir, n = synthetic_score_session(tmp_path)
    a = Namespace(session=str(sdir), source=None, also_pc=False, model=None, thresholds="0.5,0.9,0.98",
                  pad_before=0.5, pad_after=3.5, refractory=2.0, include_unaligned=False)
    (rep,) = K.score_session(a)
    kb = rep["blocks"][0]["cue_hits"]
    assert kb["kivi"]["0.5"]["n"] == n and rep["blocks"][0]["lost_cues"] == 0
    assert kb["kivi"]["0.5"]["k"] >= n // 3                # synthetic held-out recall is about 60-85 %
    assert kb["alexa"]["0.5"]["k"] <= 2                    # "kivi" is not "alexa"
    assert rep["blocks"][2]["false_accepts"]["kivi"]["0.5"]["k"] == 0 and rep["blocks"][2]["noncue_hours"] > 0
    assert rep["alignment_files"][0]["used"] and "clip" not in json.dumps(rep)
    md = K.to_markdown(rep)
    assert "Recall" in md and "Coverage and server events" in md
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    sdir2, _ = synthetic_score_session(bad_dir, ok=False)
    a.session = str(sdir2)
    (rep2,) = K.score_session(a)
    assert rep2["blocks"][0]["lost_cues"] == n and rep2["blocks"][0]["cue_hits"]["kivi"]["0.5"] == {
        **rep2["blocks"][0]["cue_hits"]["kivi"]["0.5"], "k": 0, "n": n}
    assert not rep2["alignment_files"][0]["used"] and "NOT TRUSTED" in K.to_markdown(rep2)
    a.include_unaligned = True
    (rep3,) = K.score_session(a)
    assert rep3["blocks"][0]["cue_hits"]["kivi"]["0.5"]["k"] == kb["kivi"]["0.5"]["k"]


# ---------------------------------------------------------------------------------------------
# request-audio-only runs, model labels and calibration, recall forms, exclusions, negatives
# ---------------------------------------------------------------------------------------------


def put_stt_file(tmp_path, t0, o_pc, ref, rng, a_s, b_s, err, run, prefix="bd8256", sig=None):
    """A request-audio file (`01_stt-*.wav`, no wake file in its run) holding the TV's version of the
    reference between reference seconds a_s and b_s; its listed mtime is `err` s after the true end."""
    seg = ref[int(a_s * RATE):int(b_s * RATE)]
    x = sig if sig is not None else delayed(seg, 0.0, rng, gain=10, snr_db=8)[:len(seg)]
    d = tmp_path / "rec" / (prefix + "abcdef") / "Kiwi" / str(run)
    d.mkdir(parents=True)
    f = d / "01_stt-stt.openwakeword.wav"
    f.write_bytes(C.wav_header(len(x) * 2) + x.astype("<i2").tobytes())
    end = t0 + o_pc + a_s + len(x) / RATE + err
    os.utime(f, (end, end))
    return end


def test_consistent_group_needs_two_agreeing_files():
    def r(delta, ok=True):
        return {"ok": ok, "a": 10.0 + delta, "d_prior_s": 10.0}
    assert C.consistent_group([r(-0.3), r(0.0), r(-0.35), r(0.9), r(0.0, ok=False)], tol_s=0.5) == [True, True, True, False, False]
    assert C.consistent_group([r(-0.3)]) == [False]                           # a lone file has nothing to agree with
    assert C.consistent_group([r(-0.3), r(0.4, ok=False)]) == [False, False]
    assert C.consistent_group([r(-1.0), r(0.5)], tol_s=0.5) == [False, False]


def test_extract_recovers_request_only_runs_and_is_idempotent(tmp_path, capsys):
    d, s, ref, rng, o_pc, t0 = prepared_session(tmp_path)
    wins = {(w["block"], w["cue"]): w for w in C.cue_windows(s)}
    tau = lambda key, k: wins[key][k] - o_pc                                     # noqa: E731
    # cues 1-3 of block 1 fully inside one run each (the listed end is 0.3 s late: the same latency for all),
    # cue 4 only partly (the run stops 1.2 s after the cue), the rest has no audio at all
    put_stt_file(tmp_path, t0, o_pc, ref, rng, tau((1, 1), "t0") - 0.2, tau((1, 1), "t1") + 0.2, 0.30, 1001)
    put_stt_file(tmp_path, t0, o_pc, ref, rng, tau((1, 2), "t0") - 0.2, tau((1, 2), "t1") + 0.2, 0.34, 1002)
    put_stt_file(tmp_path, t0, o_pc, ref, rng, tau((1, 3), "t0") - 0.2, tau((1, 3), "t1") + 0.2, 0.27, 1003)
    put_stt_file(tmp_path, t0, o_pc, ref, rng, tau((1, 4), "t0") - 0.2, tau((1, 4), "t0") + 1.7, 0.30, 1004)
    # an unrelated run (nobody spoke near the desk mic): not aligned, left out
    bad_start = tau((2, 1), "t0")
    put_stt_file(tmp_path, t0, o_pc, ref, rng, bad_start, bad_start + 4.0, 0.3, 1005, sig=speechlike(4.0, 99))
    ds = tmp_path / "datasets" / "real"
    args = extract_args(tmp_path, d, ds, "--now", str(t0 + 99999), "--win-s", "10")
    X.main(args)
    out = capsys.readouterr().out
    sid = s["session_id"]
    rows = C.read_jsonl(ds / "manifest.jsonl")
    assert all(set(r) == set(C.MANIFEST_FIELDS) for r in rows)
    tv = [r for r in rows if r["source"] == "tv-c3a"]
    cues = [r for r in tv if r["kind"] == "cue"]
    assert sorted((r["block"], r["cue_index"]) for r in cues) == [(1, 1), (1, 2), (1, 3)], "\n".join(l for l in out.splitlines() if "align" in l)
    assert {r["stage"] for r in tv} == {"stt"} and {r["stage"] for r in rows if r["source"] != "tv-c3a"} == {None}
    assert {r["source_run"] for r in cues} == {"Kiwi/1001", "Kiwi/1002", "Kiwi/1003"} and all(r["align_ok"] for r in cues)
    rep = json.loads(next(ds.glob(f"{sid}/extract-*.json")).read_text())
    n_cues = sum(len(b["cues"]) for b in s["blocks"])
    assert rep["per_source"]["tv-c3a"]["coverage"] == {"full": 3, "partial": 1, "none": n_cues - 4}
    assert rep["per_source"]["pc-mic"]["coverage"]["full"] == n_cues
    cc = {(c["block"], c["cue"]): c for c in rep["per_source"]["tv-c3a"]["cue_coverage"]}
    assert cc[(1, 4)]["coverage"] == "partial" and 0.2 < cc[(1, 4)]["covered_fraction"] < 0.6 and cc[(1, 4)]["stages"] == ["stt"]
    assert cc[(1, 1)]["clip_cut"] and not cc[(1, 4)]["clip_cut"]
    al = json.loads((ds / sid / "tv-c3a" / "alignment.json").read_text())["files"]
    by_run = {r["run"]: r for r in al}
    for run, a_s in (("1001", tau((1, 1), "t0") - 0.2), ("1002", tau((1, 2), "t0") - 0.2), ("1003", tau((1, 3), "t0") - 0.2)):
        r = by_run[f"Kiwi/{run}"]
        assert r["stage"] == "stt" and r["ok"] and r["included"] and r["run_end_ref_s"] is None
        assert abs(r["offset_a_s"] - (-a_s)) < 0.003, (run, r["offset_a_s"], -a_s)   # file time = ref time - a_s
    assert by_run["Kiwi/1004"]["ok"]                                              # agrees with the others
    assert by_run["Kiwi/1005"]["ok"] is False and by_run["Kiwi/1005"]["included"] is False
    assert "NOT TRUSTED" in out and "cue coverage: full 3, partial 1" in out
    raw = sorted(p.name for p in (ds / sid / "tv-c3a" / "raw").glob("*.wav"))
    assert len(raw) == 5 and all(n.startswith("stt-") for n in raw)
    # a re-run adds nothing to the manifest and no clip or raw file; only new small records appear
    n_rows = len(rows)
    clips_before = sorted(str(p) for p in (ds / sid).rglob("c0*.wav")) + sorted(str(p) for p in (ds / sid / "tv-c3a" / "raw").glob("*.wav"))
    X.main(args)
    assert len(C.read_jsonl(ds / "manifest.jsonl")) == n_rows
    assert sorted(str(p) for p in (ds / sid).rglob("c0*.wav")) + sorted(str(p) for p in (ds / sid / "tv-c3a" / "raw").glob("*.wav")) == clips_before
    # a single request-only run has nothing to agree with: left out
    (tmp_path / "second").mkdir()
    d2, s2, ref2, rng2, o2, t02 = prepared_session(tmp_path / "second", seed=4)
    w2 = C.cue_windows(s2)[0]
    put_stt_file(tmp_path / "second", t02, o2, ref2, rng2, w2["t0"] - o2 - 0.2, w2["t1"] - o2 + 0.2, 0.3, 2001)
    ds2 = tmp_path / "second" / "ds"
    X.main(extract_args(tmp_path / "second", d2, ds2, "--now", str(t02 + 99999)))
    assert not [r for r in C.read_jsonl(ds2 / "manifest.jsonl") if r["source"] == "tv-c3a"]


def test_extract_dry_run_lists_request_only_runs(tmp_path, capsys):
    d, s, ref, rng, o_pc, t0 = prepared_session(tmp_path)
    w = C.cue_windows(s)[0]
    put_stt_file(tmp_path, t0, o_pc, ref, rng, w["t0"] - o_pc - 0.2, w["t1"] - o_pc + 0.2, 0.3, 3001)
    X.main(extract_args(tmp_path, d, tmp_path / "ds", "--dry-run"))
    assert "request-audio-only run Kiwi/3001" in capsys.readouterr().out and not (tmp_path / "ds").exists()


def test_model_identity_label_and_calibration(tmp_path):
    import hashlib
    import kivi_score as K
    f = tmp_path / "v9" / "final" / "m.tflite"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"not a model, only bytes")
    sha = hashlib.sha256(b"not a model, only bytes").hexdigest()
    infos = K.model_infos([f"kivi={f}", "alexa=builtin"], {"kivi": 0.95}, "v2")
    assert infos["kivi"] == {"path": str(f.resolve()), "sha256": sha, "label": "v2", "server_0_5_raw": 0.95,
                             "calibration_source": "parameter"}
    assert infos["alexa"]["server_0_5_raw"] == 0.5 and infos["alexa"]["sha256"] is None
    # nothing is assumed: no parameter and no sidecar means no operating point
    assert K.model_infos([f"kivi={f}"])["kivi"]["server_0_5_raw"] is None
    assert K.model_infos([f"kivi={f}"])["kivi"]["label"] == "-".join(f.resolve().parts[-3:-1]) + "-m"
    Path(str(f) + ".calibration.json").write_text('{"server_0_5_raw": 0.98}')
    side = K.model_infos([f"kivi={f}"])["kivi"]
    assert side["server_0_5_raw"] == 0.98 and side["calibration_source"] == "sidecar"
    assert K.model_infos([f"kivi={f}"], {"kivi": 0.9})["kivi"]["server_0_5_raw"] == 0.9      # the parameter wins
    assert K.derive_label(WORK / "v2/final/kivi_uncalibrated.tflite") == "v2-final-kivi_uncalibrated"
    assert 0.95 in K.thresholds_with_ops("0.5,0.98", infos) and 0.9 in K.thresholds_with_ops("0.5", {})
    assert K.parse_cue_ids(["B4c5", "b1C10"]) == {(4, 5), (1, 10)}
    with pytest.raises(SystemExit):
        K.parse_cue_ids(["4-5"])
    with pytest.raises(SystemExit):
        K.parse_calibration(["kivi"])
    rep = K.analyze(fake_session([("k", "kivi", [(5.0, "kivi", "kivi")], 0.0, 30.0)]),
                    {"kivi": np.zeros(10), "alexa": np.zeros(10)}, np.arange(10) * 0.08,
                    [[0.0, 30.0]], [0.5, 0.95])
    md = K.to_markdown({**rep, "session": "t", "source": "tv-c3a", "models": infos})
    assert sha in md and str(f.resolve()) in md and "label `v2`" in md and "server 0.5 = raw 0.95" in md
    assert "At each model's server-0.5 operating point" in md


def test_recall_forms_partial_audio_and_excluded_cues():
    import kivi_score as K
    sess = fake_session([("k", "kivi", [(5.0, "kivi", "kivi"), (15.0, "kivi", "kivi"), (25.0, "kivi", "kivi"), (35.0, "kivi", "kivi")],
                          0.0, 45.0), ("n", "near", [(55.0, "near:kiwi", "kiwi")], 45.0, 65.0)])
    times = np.arange(0, 65, 0.08)
    pk = np.zeros(len(times))
    for t in (6.0, 16.0, 36.0, 56.0):
        pk[np.argmin(abs(times - t))] = 0.99                      # cues 1, 2, 4 and the near word score above 0.98
    cov = [[4.0, 9.0], [14.0, 17.0], [24.9, 25.1], [54.0, 60.0]]   # cue 1 full, cue 2 partial (word inside), cue 3 partial (no score there), cue 4 none
    spans = {(1, 1): (5.5, 6.2), (1, 2): (15.4, 16.2), (1, 3): (25.5, 26.2), (1, 4): None, (2, 1): (55.5, 56.0)}
    rep = K.analyze(sess, {"kivi": pk}, times, cov, [0.5, 0.98], word_spans=spans)
    b = rep["blocks"][0]
    assert (b["full_cues"], b["partial_cues"], b["no_audio_cues"], b["lost_cues"]) == (1, 2, 1, 3)
    assert b["cue_hits"]["kivi"]["0.5"]["k"] == 1 and b["cue_hits"]["kivi"]["0.5"]["n"] == 4        # misses counted
    assert b["cue_hits_full"]["kivi"]["0.5"] == {**b["cue_hits_full"]["kivi"]["0.5"], "k": 1, "n": 1}
    assert b["cue_hits_partial"]["kivi"]["0.5"]["k"] == 1 and b["cue_hits_partial"]["kivi"]["0.5"]["n"] == 2
    assert b["cue_hits_word"]["kivi"]["0.5"]["k"] == 2 and b["cue_hits_word"]["kivi"]["0.5"]["n"] == 2   # cue 1 and cue 2 (word inside)
    assert rep["overall"]["partial_cues"] == 2 and rep["word_spans_found"] == 4
    # an excluded cue leaves every denominator
    rep2 = K.analyze(sess, {"kivi": pk}, times, cov, [0.5], exclude_cues={(1, 4)}, word_spans=spans)
    b2 = rep2["blocks"][0]
    assert b2["cues"] == 3 and b2["no_audio_cues"] == 0 and b2["excluded_cues"] == [4] and b2["cue_hits"]["kivi"]["0.5"]["n"] == 3
    assert rep2["excluded_cues"] == [[1, 4]] and "B1c4" in K.to_markdown({**rep2, "session": "t", "source": "x", "models": {"kivi": "k"}})
    # near-word hits use the same full-audio rule
    assert rep["overall"]["near_total"]["cue_hits"]["kivi"]["0.5"]["k"] == 1


def test_word_span_from_ref_finds_the_burst_without_text():
    import kivi_score as K
    x = bursts_audio(30.0, [12.0], seed=1)
    g = K.word_span_from_ref(x, 10.0, 14.0)
    assert g is not None and abs(g[0] - 12.0) < 0.2 and abs(g[1] - 12.5) < 0.25
    assert K.word_span_from_ref(bursts_audio(30.0, [], seed=2), 10.0, 14.0) is None


def test_negatives_exclusions_session_window_and_after(tmp_path):
    ds = tmp_path / "ds"
    (ds / "sess").mkdir(parents=True)
    (ds / "sess" / "session.json").write_text(json.dumps({"t0_utc": X.utc_str(T0 + 100), "end_utc": X.utc_str(T0 + 160),
                                                         "device": "c3-a", "blocks": []}))
    win = N.session_windows(ds, margin_s=30.0)
    assert win == {"c3-a": [(T0 + 70, T0 + 190)]}
    assert N.parse_exclude_after(["c3-a=2026-11-07T11:50:00Z"]) == {"c3-a": X.parse_when("2026-11-07T11:50:00Z")}
    with pytest.raises(SystemExit):
        N.parse_exclude_after(["c3-a"])
    a = Namespace(trim_start_s=0.3, boundary_s=7.0, followup_s=60.0, exclude_s=10.0, min_seg_s=10.0)
    f = {"start": T0, "mtime": T0 + 400}
    assert N.plan_file(f, None, [], a, win["c3-a"]) == [(T0 + 0.3, T0 + 70), (T0 + 190, T0 + 393)]
    assert N.plan_file(f, None, [], a, (), after=T0 + 250) == [(T0 + 0.3, T0 + 250)]
    assert N.plan_file({"start": T0 + 300, "mtime": T0 + 400}, None, [], a, (), after=T0 + 250) == []
    # end to end: nothing of c3-a after the cut-off, nothing inside the session window, c3-b unaffected
    xa = (np.arange(400 * RATE) % 30000).astype(np.int16)
    make_wake_tree(tmp_path / "rec", "bd8256", 1, T0, xa)
    make_wake_tree(tmp_path / "rec", "fda18f", 2, T0, xa)
    ns = Namespace(dataset_root=str(ds), hours=5.0, devices=None, devices_file=None, context="x", namespace="x", pod="x",
                   container=None, recordings_root="/x", presses_file=None, no_presses=True, pg_pod="x", pg_container="x",
                   pg_user="x", pg_db="x", min_age_s=600.0, trim_start_s=0.3, boundary_s=7.0, followup_s=60.0, exclude_s=10.0,
                   min_seg_s=10.0, seed=1, now=T0 + 5000, dry_run=False, exclude_after=[f"c3-a={X.utc_str(T0 + 300)}"])
    N.run(ns, X.LocalFetcher(tmp_path / "rec"))
    for r in C.read_jsonl(ds / "negatives" / "manifest.jsonl"):
        t0_, t1_ = X.parse_utc(r["start_utc"]).timestamp(), X.parse_utc(r["end_utc"]).timestamp()
        if r["device"] == "c3-a":
            assert t1_ <= T0 + 300.001 and t0_ >= T0 + 280 - 0.001      # session 100-160 s +-120 s margin, cut-off at 300 s
    hrs = {dev: sum(X.parse_utc(r["end_utc"]).timestamp() - X.parse_utc(r["start_utc"]).timestamp()
                    for r in C.read_jsonl(ds / "negatives" / "manifest.jsonl") if r["device"] == dev) for dev in ("c3-a", "c3-b")}
    assert abs(hrs["c3-a"] - 20.0) < 1 and abs(hrs["c3-b"] - 392.7) < 1


@needs_work
def test_score_negatives_counts_detections_per_device(tmp_path):
    import kivi_score as K
    import soundfile as sf
    mk = list(csv.DictReader(open(WORK / "eval_clips/eval_kivi.manifest.csv", encoding="utf-8")))
    clips = [r["file"] for r in mk if r["text"] == "kivi"][:6]
    x = np.zeros(10 * RATE, np.int16)
    for f in clips:
        c, _ = sf.read(WORK / "eval_clips/eval_kivi" / f, dtype="int16")
        x = np.concatenate([x, c, np.zeros(3 * RATE, np.int16)])
    ds = tmp_path / "ds"
    (ds / "negatives" / "c3-a").mkdir(parents=True)
    (ds / "negatives" / "c3-b").mkdir(parents=True)
    C.write_wav_new(ds / "negatives" / "c3-a" / "1-01.wav", x)
    C.write_wav_new(ds / "negatives" / "c3-b" / "2-01.wav", np.zeros(len(x), np.int16))
    for dev, name in (("c3-a", "1-01"), ("c3-b", "2-01")):
        C.append_jsonl(ds / "negatives" / "manifest.jsonl", [C.make_row(device=dev, source=C.source_label(dev), label="negative",
                                                                        kind="negative", clip=f"negatives/{dev}/{name}.wav")])
    a = Namespace(negatives=str(ds / "negatives"), model=[f"kivi={WORK / 'final/kivi_uncalibrated.tflite'}"], calibration=["kivi=0.98"],
                  label="v1", thresholds="0.5,0.9", refractory=2.0, jobs=1)
    rep = K.score_negatives(a)
    assert abs(rep["devices"]["c3-a"]["hours"] - len(x) / RATE / 3600) < 1e-3 and rep["devices"]["c3-b"]["hours"] > 0
    assert rep["devices"]["c3-a"]["false_accepts"]["kivi"]["0.5"]["k"] >= 1               # spoken kivi counts as a detection
    assert rep["devices"]["c3-b"]["false_accepts"]["kivi"]["0.98"]["k"] == 0
    assert rep["devices"]["c3-b"]["false_accepts"]["kivi"]["0.98"]["ci95"][0] == 0.0
    assert rep["pooled"]["hours"] > rep["devices"]["c3-a"]["hours"]
    assert rep["models"]["kivi"]["label"] == "v1" and rep["models"]["kivi"]["server_0_5_raw"] == 0.98
    md = K.negatives_markdown(rep)
    assert "c3-a" in md and "pooled" in md and "sha256" in md and "negatives/c3-a" not in md


def test_kubectl_fetcher_rereads_a_short_block_and_fails_after_retries(tmp_path, monkeypatch, capsys):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    k = bindir / "kubectl"
    k.write_text('#!/bin/sh\nwhile [ "$1" != "--" ]; do shift; done\nshift\n'
                 'case "$3" in *"dd if"*) if [ -n "$FLAKY" ] && { [ ! -e "$FLAKY" ] || [ -n "$ALWAYS" ]; }; then : > "$FLAKY"; "$@" | head -c 100; exit 0; fi;; esac\n'
                 'exec "$@"\n')
    k.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    x = (np.arange(40000) % 30000).astype(np.int16)
    f = make_wake_tree(tmp_path / "rec", "bd8256", 7, T0, x)
    data = f.read_bytes()
    kf = X.KubectlFetcher("ctx", "ns", "pod", None, root=str(tmp_path / "rec"))
    kf.BLOCK = 20000                                          # several blocks
    (item,) = kf.list_run_files("bd8256", "wake")
    monkeypatch.setenv("FLAKY", str(tmp_path / "flaky1"))     # the first block is cut short once
    assert b"".join(kf.fetch_range(item["path"], 44, len(data))) == data[44:]
    assert "attempt 1 of 4" in capsys.readouterr().err
    monkeypatch.setenv("FLAKY", str(tmp_path / "flaky2"))
    monkeypatch.setenv("ALWAYS", "1")                         # every read is cut short: the copy fails
    with pytest.raises(SystemExit):
        list(kf.fetch_range(item["path"], 44, len(data)))


# ---------------------------------------------------------------------------------------------
# later decisions: splits and superseded rows (append-only marks), piecewise alignment, session time map
# ---------------------------------------------------------------------------------------------


def marks_dataset(tmp_path):
    import kivi_dataset_marks as M
    ds = tmp_path / "ds"
    ds.mkdir()
    sid = "20261107T120257Z-spk01-tv-c3a"

    def row(source, rev, split, clip):
        return C.make_row(origin="session", session=sid, source=source, speaker="spk01", split=split, kind="cue", label="kivi",
                          clip=f"{sid}/{source}/clips{('-' + rev) if rev else ''}/{clip}", revision=rev, eval_channel=source == "tv-c3a")
    rows = [row("pc-mic", "", "test", "a.wav"), row("tv-c3a", "", "test", "b.wav"), row("pc-mic", "r2", "test", "a.wav"),
            row("tv-c3a", "r2", "test", "b.wav"), C.make_row(origin="session", session="other", source="tv-c3a", speaker="spk01",
                                                              split="unassigned", clip="other/tv-c3a/clips/x.wav", revision="")]
    C.append_jsonl(ds / "manifest.jsonl", rows)
    return M, ds, sid


def test_superseded_rows_and_late_splits_are_marks_not_edits(tmp_path):
    M, ds, sid = marks_dataset(tmp_path)
    before = (ds / "manifest.jsonl").read_bytes()
    M.main(["supersede", "--dataset-root", str(ds), "--session", sid, "--revision", "", "--by", "r2", "--reason", "bad time map"])
    M.main(["split", "--dataset-root", str(ds), "--session", "other", "--split", "train", "--reason", "first session trains"])
    assert (ds / "manifest.jsonl").read_bytes() == before                         # the manifest itself is untouched
    rows = C.read_manifest(ds)
    assert [r["superseded"] for r in rows] == [True, True, False, False, False]
    assert all(r["revision"] == "" for r in rows if r["superseded"]) and len(rows) == 5
    other = rows[-1]
    assert other["split"] == "train" and other["split_in_manifest"] == "unassigned"
    # a train decision on a session gives its desk channels train-only, a test decision keeps them test
    assert C.channel_split("train", "pc-mic") == "train-only" and C.channel_split("test", "pc-mic") == "test"
    # superseded rows can never train, even with a train split; non-superseded train rows can
    C.append_jsonl(ds / "splits.jsonl", [{"session": sid, "split": "train", "reason": "x", "set_utc": "t"}])
    tr = C.training_rows(C.read_manifest(ds))
    assert {(r["session"], r["revision"]) for r in tr} == {(sid, "r2"), ("other", "")}
    assert not [r for r in tr if r["superseded"]]
    assert {(r["session"], r["revision"]) for r in C.training_rows(rows)} == {("other", "")}
    # the marks tool refuses what it cannot match
    with pytest.raises(SystemExit):
        M.main(["supersede", "--dataset-root", str(ds), "--session", sid, "--revision", "r9", "--by", "r10", "--reason", "x"])
    with pytest.raises(SystemExit):
        M.main(["split", "--dataset-root", str(ds), "--session", "nosuch", "--split", "test", "--reason", "x"])
    assert stat.S_IMODE((ds / "superseded.jsonl").stat().st_mode) == 0o600


def test_scorer_skips_superseded_alignment_and_negatives(tmp_path):
    import kivi_score as K
    sdir = tmp_path / "ds" / "sessA"
    (sdir / "tv-c3a").mkdir(parents=True)
    old = {"extract_id": "ex-1", "files": [{"name": "o.wav", "ok": True, "included": True, "offset_a_s": 0.0, "coverage_s": [0, 1]}]}
    new = {"extract_id": "ex-2", "ref_map": "progress", "files": [{"name": "n.wav", "ok": True, "included": True, "offset_a_s": 0.0,
                                                                  "coverage_s": [0, 1]}]}
    (sdir / "extract-ex-1.json").write_text(json.dumps({"revision": ""}))
    (sdir / "extract-ex-2.json").write_text(json.dumps({"revision": "r2"}))
    (sdir / "tv-c3a" / "alignment-ex-1.json").write_text(json.dumps(old))
    (sdir / "tv-c3a" / "alignment-ex-2.json").write_text(json.dumps(new))
    os.utime(sdir / "tv-c3a" / "alignment-ex-1.json", (T0 + 100, T0 + 100))
    os.utime(sdir / "tv-c3a" / "alignment-ex-2.json", (T0 + 50, T0 + 50))         # the OLD revision is the newest file
    al, meta = K.pick_alignment(sdir, "tv-c3a")
    assert meta["revision"] == "" and meta["extract_id"] == "ex-1"
    C.append_jsonl(tmp_path / "ds" / "superseded.jsonl", [{"session": "sessA", "revision": "", "source": None,
                                                           "superseded_by": "r2", "reason": "x", "set_utc": "t"}])
    al, meta = K.pick_alignment(sdir, "tv-c3a")
    assert meta["revision"] == "r2" and meta["ref_map"] == "progress" and meta["superseded_alignment_files_skipped"] == ["alignment-ex-1.json"]
    md = K.to_markdown({**K.analyze(fake_session([("k", "kivi", [(5.0, "kivi", "kivi")], 0.0, 30.0)]), {"kivi": np.zeros(5)},
                                    np.arange(5) * 0.08, [[0, 30.0]], [0.5]),
                        "session": "t", "source": "tv-c3a", "models": {"kivi": "k"}, "scored_from": meta})
    assert "extraction revision `r2`" in md and "alignment-ex-1.json" in md
    C.append_jsonl(tmp_path / "ds" / "superseded.jsonl", [{"session": "sessA", "revision": "r2", "source": "tv-c3a",
                                                           "superseded_by": "r3", "reason": "x", "set_utc": "t"}])
    with pytest.raises(SystemExit):
        K.pick_alignment(sdir, "tv-c3a")                                          # nothing left to score
    # superseded negatives are not scored
    neg = tmp_path / "ds" / "negatives"
    neg.mkdir()
    C.append_jsonl(neg / "manifest.jsonl", [C.make_row(device="c3-a", clip="negatives/c3-a/1.wav", revision="")])
    C.append_jsonl(neg / "superseded.jsonl", [{"session": None, "revision": "", "source": None, "superseded_by": "x", "reason": "x", "set_utc": "t"}])
    assert [r for r in C.read_manifest(neg) if not r["superseded"]] == []


def test_piecewise_alignment_when_the_reference_clock_wanders():
    """A reference recorder that ran slow for a while (its audio is a time-warped copy: it falls 0.25 s behind
    between 60 s and 80 s, a dropout-like stretch): one offset line does not fit, the smooth offset curve is
    accepted (when allowed) and maps file time back to reference time within a few ms. Unrelated audio never passes."""
    rng = np.random.default_rng(8)
    ref_full = speechlike(120.0, 31)
    t = np.arange(len(ref_full)) / RATE

    def warp(tau):
        return 0.25 * np.clip((np.asarray(tau) - 60.0) / 20.0, 0.0, 1.0)
    ref = np.interp(t + warp(t), t, ref_full.astype(np.float32)).astype(np.int16)    # ref[tau] = ref_full[tau + warp(tau)]
    sig = delayed(ref_full, 1.0, rng, gain=8, snr_db=14)                             # file time = ref_full time + 1
    lin = C.align_stream(ref, sig, 1.05, coarse_lag_s=3.0, refine_lag_s=1.5, win_s=10.0)
    line_err = max(abs(float(C.curve_ref_time(None, lin["a"], lin["b"], tau + float(warp(tau)) + 1.0)) - tau) for tau in (20.0, 105.0))
    assert lin["ok"] is False or line_err > 0.04                                    # one line is no good for this clock
    cur = C.align_stream(ref, sig, 1.05, coarse_lag_s=3.0, refine_lag_s=1.5, win_s=10.0, allow_curve=True)
    assert cur["ok"] and cur["mode"] == "piecewise" and len(cur["curve"]) >= 8
    for tau in (20.0, 105.0):
        got = float(C.curve_ref_time(cur["curve"], cur["a"], cur["b"], tau + float(warp(tau)) + 1.0))
        assert abs(got - tau) < 0.03, (tau, got)
    junk = C.align_stream(ref, speechlike(120.0, 77), 1.0, coarse_lag_s=3.0, refine_lag_s=1.5, win_s=10.0, allow_curve=True)
    assert junk["ok"] is False
    assert not C.smooth_curve([0, 15, 30, 45, 60, 75], [0.0, 0.7, -0.3, 0.9, 0.1, -0.6])
    assert C.smooth_curve([0, 15, 30, 45, 60, 75], [0.0, 0.01, 0.02, 0.1, 0.15, 0.16])


def test_session_to_ref_follows_the_sample_count_only_after_a_dropout():
    sess = fake_session([("k", "kivi", [(5.0, "kivi", "kivi")], 0.0, 30.0)])
    sess["sources"]["pc-mic"]["first_sample_offset_s"] = 0.5
    # small creep (0.1 s over the session): the plain offset is kept
    sess["sources"]["pc-mic"]["progress"] = [[0.6 + 10 * i, 1600 + 160000 * i - int(0.0003 * RATE * i)] for i in range(6)]
    f, mode = C.session_to_ref(sess)
    assert mode == "offset" and float(f(20.0)) == 19.5
    # a dropout of 0.6 s between 20 s and 30 s: later cues move back by that much
    prog = [[0.6 + 10 * i, 1600 + 160000 * i - (int(0.6 * RATE) if i >= 3 else 0)] for i in range(6)]
    sess["sources"]["pc-mic"]["progress"] = prog
    f, mode = C.session_to_ref(sess)
    assert mode == "progress" and abs(float(f(5.0)) - 4.5) < 1e-6 and abs(float(f(50.0)) - (50.0 - 0.5 - 0.6)) < 1e-6


def test_negatives_exclude_between_closes_a_window(tmp_path):
    assert N.parse_exclude_between(["c3-a=2026-11-07T11:50:00Z,2026-11-07T12:24:40Z"]) == {
        "c3-a": [(X.parse_when("2026-11-07T11:50:00Z"), X.parse_when("2026-11-07T12:24:40Z"))]}
    with pytest.raises(SystemExit):
        N.parse_exclude_between(["c3-a=2026-11-07T12:00:00Z"])
    with pytest.raises(SystemExit):
        N.parse_exclude_between(["c3-a=2026-11-07T12:30:00Z,2026-11-07T12:00:00Z"])
    a = Namespace(trim_start_s=0.3, boundary_s=7.0, followup_s=60.0, exclude_s=10.0, min_seg_s=10.0)
    f = {"start": T0, "mtime": T0 + 600}
    # audio before and after the window is usable again, nothing inside it
    assert N.plan_file(f, None, [], a, [(T0 + 200, T0 + 300)]) == [(T0 + 0.3, T0 + 200), (T0 + 300, T0 + 593)]


def test_session_cue_wording_says_silent_not_off():
    import kivi_session as S2
    text = S2.instruction({"kind": "kivi", "n": 3, "conditions": {"position_m": 1.5, "tv": "off"}})
    assert "TV SILENT" in text and "keep the TV on" in text and "TV OFF" not in text


# ---------------------------------------------------------------------------------------------
# wake phrase (default "kivi"; "oye kivi" for the fallback phrase)
# ---------------------------------------------------------------------------------------------


def test_session_phrase_default_and_oye_kivi(tmp_path, capsys):
    d, s = dry_session(tmp_path)
    assert s["phrase"] == "kivi" and {c["label"] for c in s["blocks"][0]["cues"]} == {"kivi"}
    capsys.readouterr()
    blocks = S.default_blocks("oye kivi")
    near = next(b for b in blocks if b["kind"] == "near")
    assert near["words"] == ["kivi", "oye kili", "oye kiri", "oye tivi", "oye mira", "oye tú", "hoy vi", "oye Fifi", "o sea"]
    assert near["each"] == 2 and blocks[0]["name"].startswith("oye-kivi-")
    assert [b["words"] for b in S.default_blocks() if b["kind"] == "near"] == [S.NEAR_WORDS]       # default unchanged
    assert S.default_blocks("  Oye   KIVI ") == blocks                                              # normalized
    with pytest.raises(SystemExit):
        S.default_blocks("hola kivi")                    # no near-word list for it: give --blocks
    with pytest.raises(SystemExit):
        S.default_blocks("oye kivi 2")                   # letters only
    assert "say OYE KIVI 10 times" in S.instruction(blocks[0], "oye kivi")
    out = tmp_path / "sessions2"
    S.main(["--dry-run", "--phrase", "oye kivi", "--out-dir", str(out), "--seed", "3", "--only", "oye-kivi-1.5m-tvoff",
            "--only", "near-words"])
    (sd,) = list(out.iterdir())
    s = json.loads((sd / "session.json").read_text(encoding="utf-8"))
    assert s["phrase"] == "oye kivi"
    pos = [c for b in s["blocks"] if b["kind"] == "kivi" for c in b["cues"]]
    assert {c["label"] for c in pos} == {"oye kivi"}
    assert {c["label"] for b in s["blocks"] if b["kind"] == "near" for c in b["cues"]} >= {"near:kivi", "near:oye kili", "near:o sea"}
    assert "say OYE KIVI now" in capsys.readouterr().out


def test_training_rows_filter_by_phrase_and_old_rows_are_kivi():
    rows = [{"speaker": "spk01", "split": "train", "label": "kivi", "kind": "cue"},                         # old row: no phrase
            {"speaker": "spk01", "split": "train", "label": "near:kili", "kind": "cue", "phrase": "kivi"},
            {"speaker": "spk01", "split": "train", "label": "background", "kind": "segment", "phrase": "kivi"},
            {"speaker": "spk01", "split": "train", "label": "oye kivi", "kind": "cue", "phrase": "oye kivi"},
            {"speaker": "spk01", "split": "train", "label": "near:kivi", "kind": "cue", "phrase": "oye kivi"},
            {"speaker": "spk01", "split": "test", "label": "kivi", "kind": "cue"},
            {"speaker": "", "split": "train", "label": "negative", "kind": "negative"}]
    assert C.row_phrase(rows[0]) == "kivi" and C.row_phrase(rows[6]) is None and C.row_phrase(rows[3]) == "oye kivi"
    assert len(C.training_rows(rows)) == 6                                           # no phrase: unchanged
    assert [r["label"] for r in C.training_rows(rows, phrase="kivi")] == ["kivi", "near:kili", "background", "negative"]
    assert [r["label"] for r in C.training_rows(rows, phrase=" Oye  Kivi ")] == ["oye kivi", "near:kivi", "negative"]
    neg = C.negative_rows_for_phrase(rows, "oye kivi")
    assert [r["label"] for r in neg] == ["kivi", "near:kili", "background", "near:kivi"]   # old kivi takes, and now also the
    # oye-kivi session's own near-word cue ("kivi" spoken alone) are negatives for "oye kivi"
    assert {r["negative_for"] for r in neg} == {"oye kivi"} and neg[0]["label_original"] == "kivi"
    assert [r["label"] for r in C.negative_rows_for_phrase(rows, "kivi")] == ["near:kili", "background", "oye kivi"]   # its own
    # "kivi" takes stay out; the near-word cue whose word IS "kivi" stays out too (the word check)
    assert C.negative_rows_for_phrase(rows, "oye kivi", exclude_speakers=["spk01"]) == []
    assert rows[0] == {"speaker": "spk01", "split": "train", "label": "kivi", "kind": "cue"}   # inputs are not modified


def test_extract_and_manifest_carry_the_phrase(tmp_path):
    x = bursts_audio(60, [20.0], seed=4)
    make_wake_tree(tmp_path / "rec", "fda18f", 111, T0, x)
    X.main(button_args(tmp_path, [T0 + 22.0], "--phrase", "oye kivi", ds="ds1"))
    X.main(button_args(tmp_path, [T0 + 22.0], ds="ds2"))
    (r1,) = C.read_manifest(tmp_path / "ds1")
    (r2,) = C.read_manifest(tmp_path / "ds2")
    assert (r1["phrase"], r1["label"], r1["word"]) == ("oye kivi", "oye kivi", "oye kivi")
    assert (r2["phrase"], r2["label"]) == ("kivi", "kivi")
    assert json.loads(next((tmp_path / "ds1").glob("button-*/session.json")).read_text())["phrase"] == "oye kivi"
    assert C.read_manifest(tmp_path / "ds1")[0]["clip"].endswith("p001-oye-kivi.wav")
    with pytest.raises(SystemExit):
        X.main(button_args(tmp_path, [T0 + 22.0], "--phrase", "oye kivi!", ds="ds3"))


def test_split_mark_reaches_button_rows_and_superseded_rows_are_skipped(tmp_path):
    import kivi_dataset_marks as M
    x = bursts_audio(60, [20.0], seed=4)
    make_wake_tree(tmp_path / "rec", "fda18f", 111, T0, x)
    X.main(button_args(tmp_path, [T0 + 18.0], "--button-order", "press-then-say"))      # revision "", wrong order
    X.main(button_args(tmp_path, [T0 + 22.0]))                                            # say-then-press (stp3)
    ds = tmp_path / "ds"
    sid = C.read_manifest(ds)[0]["session"]
    M.main(["supersede", "--dataset-root", str(ds), "--session", sid, "--revision", "", "--by", X.SAY_THEN_PRESS_REVISION,
            "--reason", "wrong order"])
    M.main(["split", "--dataset-root", str(ds), "--session", sid, "--split", "train", "--reason", "owner decision"])
    rows = C.read_manifest(ds)
    assert {(r["revision"], r["superseded"], r["split"]) for r in rows} == {("", True, "train"), (X.SAY_THEN_PRESS_REVISION, False, "train")}
    assert [r["revision"] for r in C.training_rows(rows)] == [X.SAY_THEN_PRESS_REVISION]
    M.main(["exclude", "--dataset-root", str(ds), "--session", sid, "--revision", X.SAY_THEN_PRESS_REVISION,
            "--cue-index", "1", "--reason", "no speech"])
    rows = C.read_manifest(ds)
    assert [(r["revision"], r["excluded"]) for r in rows] == [("", False), (X.SAY_THEN_PRESS_REVISION, True)]
    assert C.training_rows(rows) == []
    with pytest.raises(SystemExit):
        M.main(["exclude", "--dataset-root", str(ds), "--session", sid, "--revision", "nope", "--cue-index", "1", "--reason", "x"])


def test_exclude_mark_can_target_a_whole_block(tmp_path):
    """A near-word block whose cues were never spoken: `cue_index` alone would also hit the positive blocks
    (they number their cues from 1), so `--block` names the block instead. Segments are never covered."""
    import kivi_dataset_marks as M
    rows = [{"session": "s", "kind": "cue", "block_name": "oye-kivi-1.5m-tvoff", "cue_index": 1, "split": "train"},
            {"session": "s", "kind": "cue", "block_name": "oye-kivi-1.5m-tvoff", "cue_index": 2, "split": "train"},
            {"session": "s", "kind": "cue", "block_name": "near-words", "cue_index": 1, "split": "train"},
            {"session": "s", "kind": "cue", "block_name": "near-words", "cue_index": 2, "split": "train"},
            {"session": "s", "kind": "segment", "block_name": "near-words", "cue_index": None, "split": "train"},
            {"session": "other", "kind": "cue", "block_name": "near-words", "cue_index": 1, "split": "train"}]
    (tmp_path / "manifest.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert not any(r["excluded"] for r in C.read_manifest(tmp_path))          # no marks file yet
    M.main(["exclude", "--dataset-root", str(tmp_path), "--session", "s", "--block", "near-words",
            "--reason", "not spoken"])
    kept = [(r["block_name"], r["cue_index"], r["excluded"]) for r in C.read_manifest(tmp_path)]
    assert kept == [("oye-kivi-1.5m-tvoff", 1, False), ("oye-kivi-1.5m-tvoff", 2, False),
                    ("near-words", 1, True), ("near-words", 2, True), ("near-words", None, False),
                    ("near-words", 1, False)]                        # the other session is untouched
    M.main(["exclude", "--dataset-root", str(tmp_path), "--session", "s", "--block", "oye-kivi-1.5m-tvoff",
            "--cue-index", "2", "--reason", "one take"])
    assert [r["excluded"] for r in C.read_manifest(tmp_path)] == [False, True, True, True, False, False]
    with pytest.raises(SystemExit):                                  # a block that does not exist
        M.main(["exclude", "--dataset-root", str(tmp_path), "--session", "s", "--block", "nope", "--reason", "x"])
    with pytest.raises(SystemExit):                                  # and neither filter
        M.main(["exclude", "--dataset-root", str(tmp_path), "--session", "s", "--reason", "x"])


def test_score_button_takes_uses_the_extraction_windows(tmp_path, monkeypatch):
    import kivi_score as K
    x = bursts_audio(60, [20.0, 30.0], seed=6)
    make_wake_tree(tmp_path / "rec", "fda18f", 111, T0, x)
    X.main(button_args(tmp_path, [T0 + 22.0, T0 + 32.0, T0 + 50.0]))                  # takes 1, 2, and 3 (no speech)
    sdir = next((tmp_path / "ds").glob("button-*"))
    peaks = {21.0: 0.99, 31.5: 0.6}                                                    # score peaks (file seconds)

    def fake_score(feats, models, pcm):
        t = np.arange(1, len(pcm) // 1280 + 1) * 0.08
        p = np.zeros(len(t), np.float32)
        for at, v in peaks.items():
            p[np.argmin(abs(t - at - (X.parse_when(json.loads(next(sdir.glob("extract-*.json")).read_text())["raw_files"][0]["t0_utc"]) - T0)))] = v
        return {"m": p}, t
    monkeypatch.setattr(K, "score_pcm", fake_score)
    monkeypatch.setattr(K, "load_models", lambda specs: (None, {"m": None}))
    a = Namespace(button_takes=str(sdir), revision=None, exclude_take=[3], model=["m=builtin"], calibration=None, label=None,
                  thresholds="0.5,0.9")
    rep = K.score_button_takes(a)
    assert rep["takes"] == 3 and rep["revision"] == X.SAY_THEN_PRESS_REVISION
    r = rep["recall"]["m"]
    assert (r["0.9"]["all_takes"]["k"], r["0.9"]["all_takes"]["n"]) == (1, 3)           # take 1 hits, take 2 peaks at 0.6
    assert (r["0.5"]["all_takes"]["k"], r["0.5"]["without_excluded"]["n"]) == (2, 2)     # at 0.5 take 2 hits; take 3 left out
    assert "button takes" in K.button_takes_markdown(rep)
