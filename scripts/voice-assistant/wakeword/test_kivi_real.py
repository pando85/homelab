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


def test_no_deletion_or_overwrite_code_in_the_dataset_tools():
    banned = re.compile(r"os\.(remove|unlink|rmdir|replace|rename)|shutil\.(rmtree|move)|\.unlink\(|rmtree")
    for name in ("kivi_extract.py", "kivi_real_common.py", "kivi_score.py", "kivi_import_phone.py", "kivi_negatives.py"):
        text = (HERE / name).read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert not banned.search(code), name


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


def test_extract_end_to_end(tmp_path):
    d, s = dry_session(tmp_path)
    ref = speechlike(len(C.read_pcm16(d / "pc-mic.wav")) / RATE, 21)
    rng = np.random.default_rng(5)
    web = delayed(ref, 0.0123, rng)[:len(ref)]
    for name, x in (("pc-mic.wav", ref), ("webcam-mic.wav", web)):
        (d / name).chmod(0o600)
        (d / name).unlink()
        C.write_wav_new(d / name, x)
    # a satellite file: it started 1.84 s before the desk recording; its mtime is 0.7 s off
    d_true = 1.84
    tv = delayed(ref, d_true, rng, gain=10, snr_db=8, tail_s=4)
    o_pc = C.session_pc_offset(s)
    t0 = X.parse_utc(s["t0_utc"]).timestamp()
    end_true = t0 + (o_pc - d_true) + len(tv) / RATE
    rec = tmp_path / "rec" / "bd8256abcdef" / "Kiwi" / "123456789"
    rec.mkdir(parents=True)
    f = rec / "00_wake-wake_word.openwakeword.wav"
    hdr = C.wav_header(0)
    f.write_bytes(hdr[:4] + b"\xff\xff\xff\xff" + hdr[8:40] + b"\xff\xff\xff\xff" + tv.tobytes())
    os.utime(f, (end_true + 0.7, end_true + 0.7))
    other = tmp_path / "rec" / "fda18fffff" / "Kiwi" / "999"  # another satellite: must be ignored
    other.mkdir(parents=True)
    (other / "00_wake-x.wav").write_bytes(hdr + b"\x00\x00" * 1000)
    ds = tmp_path / "datasets" / "real"
    args = ["--session-dir", str(d), "--dataset-root", str(ds), "--from-dir", str(tmp_path / "rec"), "--win-s", "10"]

    X.main(args + ["--dry-run"])
    assert not ds.exists()  # nothing written

    X.main(args)
    sid = s["session_id"]
    rows = C.read_jsonl(ds / "manifest.jsonl")
    sources = {r["source"] for r in rows}
    assert sources == {"tv-c3a", "pc-mic", "webcam-mic"}
    n_cues = sum(len(b["cues"]) for b in s["blocks"])
    for src in sources:
        cues = [r for r in rows if r["source"] == src and r["kind"] == "cue"]
        assert len(cues) == n_cues == 8
        assert {r["label"] for r in cues} == {"kivi", "near:kiwi", "near:aquí vi"}
    labels = {r["label"] for r in rows}
    assert "silence" in labels
    r0 = next(r for r in rows if r["source"] == "tv-c3a" and r["kind"] == "cue")
    assert r0["device"] == "c3-a" and r0["session_device"] == "c3-a" and r0["split"] == "unassigned"
    assert r0["speaker"] == "" and r0["source_run"] == "Kiwi/123456789" and r0["align_ok"]
    assert abs(r0["align_offset_s"] - d_true) < 0.003, r0["align_offset_s"]
    assert next(r for r in rows if r["source"] == "pc-mic")["device"] == "pc-mic"
    # split policy: PC channels of an unassigned session are train-only, the satellite channel is the eval channel
    assert {r["split"] for r in rows if r["source"] == "pc-mic"} == {"train-only"}
    assert {r["split"] for r in rows if r["source"] == "tv-c3a"} == {"unassigned"}
    assert {r["eval_channel"] for r in rows if r["source"] == "tv-c3a"} == {True}
    assert {r["eval_channel"] for r in rows if r["source"] != "tv-c3a"} == {False}
    assert all(set(r) == set(C.MANIFEST_FIELDS) for r in rows)
    assert (ds / sid / "tv-c3a" / "raw" / "123456789.wav").exists()
    assert (ds / sid / "tv-c3a" / "timeline.wav").exists() and (ds / sid / "session.json").exists()
    assert (ds / sid / "pc-mic" / "raw" / "pc-mic.wav").exists()
    # permissions
    for p in [ds, ds / sid, ds / sid / "tv-c3a", ds / sid / "tv-c3a" / "clips"]:
        assert stat.S_IMODE(p.stat().st_mode) == 0o700, p
    for p in ds.rglob("*"):
        assert stat.S_IMODE(p.stat().st_mode) in (0o700, 0o600), p
    # the satellite clip and the desk clip hold the same content at the same time
    tvc = next(r for r in rows if r["source"] == "tv-c3a" and r["kind"] == "cue" and r["label"] == "kivi")
    pcc = next(r for r in rows if r["source"] == "pc-mic" and r["clip"].split("/", 2)[2] == tvc["clip"].split("/", 2)[2])
    a_, b_ = C.read_pcm16(ds / tvc["clip"]), C.read_pcm16(ds / pcc["clip"])
    assert len(a_) == len(b_)
    lag, conf = C.gcc_phat(b_, np.concatenate([np.zeros(800, np.int16), a_, np.zeros(800, np.int16)]), 800)
    assert abs(lag) < 0.002 and conf > 8, (lag, conf)
    # a second run adds no duplicate rows and overwrites nothing
    X.main(args)
    rows2 = C.read_jsonl(ds / "manifest.jsonl")
    assert len(rows2) == len(rows)
    # the manifest holds no audio paths outside the dataset and no household names
    assert all(not r["clip"].startswith("/") for r in rows2)


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


def test_score_aggregation_with_synthetic_probabilities():
    import kivi_score as K
    sess = fake_session([("k", "kivi", [(5.0, "kivi", "kivi"), (15.0, "kivi", "kivi")], 0.0, 30.0),
                         ("n", "near", [(35.0, "near:kiwi", "kiwi"), (45.0, "near:tivi", "tivi")], 30.0, 55.0),
                         ("s", "silence", [], 55.0, 65.0)])
    times = np.arange(0, 65, 0.08)
    pk = np.zeros(len(times))
    pk[np.argmin(abs(times - 6.0))] = 0.99     # cue 1 detected at 0.98
    pk[np.argmin(abs(times - 20.0))] = 0.975   # gap between cues: a false accept at 0.9 (cue 2 window ends 18.5)
    pk[np.argmin(abs(times - 36.0))] = 0.96    # near-word kiwi
    pk[np.argmin(abs(times - 60.0))] = 0.91    # in silence
    pa = np.zeros(len(times))
    rep = K.analyze(sess, {"kivi": pk, "alexa": pa}, times, [[0.0, 65.0]], [0.5, 0.9, 0.97, 0.98, 0.99])
    kb = rep["blocks"][0]["cue_hits"]["kivi"]
    assert (kb["0.5"]["k"], kb["0.5"]["n"]) == (2 - 1, 2)
    assert kb["0.98"]["k"] == 1 and kb["0.99"]["k"] == 0
    assert rep["blocks"][0]["cue_hits"]["alexa"]["0.5"]["k"] == 0
    nb = rep["blocks"][1]["by_word"]
    assert nb["kiwi"]["kivi"]["0.9"]["k"] == 1 and nb["tivi"]["kivi"]["0.9"]["k"] == 0
    fa = rep["overall"]["false_accepts"]["kivi"]
    assert fa["0.9"]["k"] == 2 and fa["0.97"]["k"] == 1 and fa["0.99"]["k"] == 0, fa
    assert abs(fa["0.9"]["hours"] - (65 - 4 * 4.0) / 3600) < 1e-3  # 4 cue windows of 4 s excluded
    assert rep["overall"]["false_accepts"]["alexa"]["0.5"]["k"] == 0
    # uncovered audio is left out
    rep2 = K.analyze(sess, {"kivi": pk}, times, [[0.0, 10.0]], [0.5])
    assert rep2["blocks"][0]["uncovered_cues"] == 1
    md = K.to_markdown({**rep, "session": "t", "source": "tv-c3a", "models": {"kivi": "k.onnx", "alexa": "a.onnx"}})
    assert "Near-word" in md and "False accepts per hour" in md


needs_work = pytest.mark.skipif(not (WORK / "final/kivi_uncalibrated.onnx").exists()
                                or not (WORK / "emb_s_melrow.onnx").exists(), reason="no kivi work directory")


@needs_work
def test_piecewise_embeddings_equal_one_shot():
    import kivi_score as K
    F = K.CappedFeatures(WORK / "emb_s_melrow.onnx", K.OWW_MODELS / "embedding_model.onnx", cpu=True)
    x = speechlike(20, 31)
    whole = K.stream_embeddings(F, x, piece_chunks=10 ** 6)
    pieces = K.stream_embeddings(F, x, piece_chunks=60, ctx_chunks=40)
    assert whole.shape == pieces.shape and np.abs(whole.astype(np.float32) - pieces.astype(np.float32)).max() < 0.05


@needs_work
def test_scorer_on_held_out_synthetic_clips(tmp_path):
    """kivi on held-out synthetic 'kivi' clips, near-word clips and silence, through the whole
    path: dataset session -> alignment.json/timeline.wav -> scorer."""
    import kivi_score as K
    import soundfile as sf
    mk = list(csv.DictReader(open(WORK / "eval_clips/eval_kivi.manifest.csv", encoding="utf-8")))
    nm = list(csv.DictReader(open(WORK / "eval_clips/eval_nearmiss.manifest.csv", encoding="utf-8")))
    kivi_clips = [r["file"] for r in mk if r["text"] == "kivi"][:14]
    near_clips = [(r["file"], r["text"]) for r in nm][:6]
    x = np.zeros(3 * RATE, np.int16)
    cues_k, cues_n = [], []
    for i, f in enumerate(kivi_clips, 1):
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
    (sdir / "tv-c3a").mkdir(parents=True)
    (sdir / "session.json").write_text(json.dumps(sess))
    C.write_wav_new(sdir / "tv-c3a" / "timeline.wav", x)
    (sdir / "tv-c3a" / "alignment.json").write_text(json.dumps({"timeline": "timeline.wav", "coverage_s": [[0, len(x) / RATE]]}))
    a = Namespace(session=str(sdir), source=None, also_pc=False, model=None, mel_rowmax=None, embedding_model=None,
                  thresholds="0.5,0.9,0.98", pad_before=0.5, pad_after=3.5, refractory=2.0, cpu=True)
    (rep,) = K.score_session(a)
    kb = rep["blocks"][0]["cue_hits"]
    assert kb["kivi"]["0.5"]["n"] == len(kivi_clips)
    assert kb["kivi"]["0.5"]["k"] >= len(kivi_clips) // 3   # synthetic held-out recall is about 60-85 %
    assert kb["alexa"]["0.5"]["k"] <= 2                      # "kivi" is not "alexa"
    assert rep["blocks"][2]["false_accepts"]["kivi"]["0.5"]["k"] == 0  # digital silence
    assert rep["blocks"][2]["noncue_hours"] > 0
    md = K.to_markdown(rep)
    assert "Recall" in md
    assert "clip" not in json.dumps(rep) and "path" not in json.dumps(rep)  # aggregates only


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
    with pytest.raises(SystemExit):               # consent is required
        P.main(base)
    with pytest.raises(SystemExit):               # names are refused
        P.main([str(flac), "--speaker", "Maria", "--consent-confirmed", "--dataset-root", str(ds)])
    P.main(base + ["--dry-run"])
    assert not ds.exists()
    assert P.main(base + ["--consent-confirmed", "--expect", "7"]) is None   # mismatch: nothing written
    assert not ds.exists()
    P.main(base + ["--consent-confirmed", "--expect", "6", "--near-labels", "kiwi,Tivi"])
    rows = C.read_jsonl(ds / "manifest.jsonl")
    assert [r["label"] for r in rows] == ["kivi"] * 4 + ["near:kiwi", "near:tivi"]
    assert {r["source"] for r in rows} == {"phone"} and {r["speaker"] for r in rows} == {"spk02"}
    assert {r["split"] for r in rows} == {"train-only"} and all(r["consent_confirmed"] for r in rows)
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


def test_button_takes(tmp_path):
    x = bursts_audio(120, [22.0, 53.5, 100.0], seed=7)    # presses at 20 s and 50 s hit; the one at 80 s has no speech
    make_wake_tree(tmp_path / "rec", "fda18f", 111, T0, x)
    presses = [T0 + 20.0, T0 + 50.0, T0 + 80.0, T0 + 500.0]
    pf = tmp_path / "presses.json"
    pf.write_text(json.dumps([X.utc_str(t) for t in presses]))
    ds = tmp_path / "ds"
    args = ["--button-takes", "--device", "c3-b", "--speaker", "spk02", "--since", X.utc_str(T0 - 60),
            "--until", X.utc_str(T0 + 600), "--presses-file", str(pf), "--dataset-root", str(ds),
            "--from-dir", str(tmp_path / "rec")]
    X.main(args)
    rows = C.read_jsonl(ds / "manifest.jsonl")
    assert [r["label"] for r in rows] == ["kivi", "kivi"]
    assert {r["source"] for r in rows} == {"tv-c3b"} and {r["device"] for r in rows} == {"c3-b"}
    assert {r["speaker"] for r in rows} == {"spk02"} and {r["origin"] for r in rows} == {"button"}
    assert [r["cue_index"] for r in rows] == [1, 2]
    assert 1.5 < rows[0]["onset_after_press_s"] < 2.5 and 3.0 < rows[1]["onset_after_press_s"] < 4.0
    n = len(C.read_pcm16(ds / rows[0]["clip"])) / RATE
    assert 1.8 < n < 2.8
    rep = json.loads(next((ds).glob("button-*/extract-*.json")).read_text())
    assert (rep["presses"], rep["clips"], rep["no_clear_onset"], rep["no_file"]) == (4, 2, 1, 1)
    X.main(args)                                            # idempotent
    assert len(C.read_jsonl(ds / "manifest.jsonl")) == 2


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
