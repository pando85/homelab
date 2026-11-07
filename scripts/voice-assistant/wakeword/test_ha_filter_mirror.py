"""Light tests of ha_filter_mirror.py with synthetic signals only (no recorded audio).

    cd scripts/voice-assistant/wakeword && /work/workspaces/kivi-wakeword/venv-hafilter/bin/python -m pytest test_ha_filter_mirror.py -q

The speex tests need `pyspeex-noise`; the aec_offline tests need the binary (`HA_FILTER_TOOL` or the
default build path). Both are skipped when missing.
"""
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ha_filter_mirror as M  # noqa: E402
import kivi_real_common as C  # noqa: E402

TOOL = os.environ.get("HA_FILTER_TOOL", "/work/workspaces/kivi-wakeword/v2/fe_build/release/examples/aec_offline")


def has_speex():
    try:
        import pyspeex_noise
        return pyspeex_noise is not None
    except ImportError:
        return False


def tone(n, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(n) / M.RATE
    # broadband bursts (on half of every 0.8 s) over a quiet floor: a speech-like envelope, unambiguous lag
    burst = np.convolve(rng.standard_normal(n), np.hanning(9), "same")
    x = 3000 * burst * (np.sin(2 * np.pi * 1.25 * t) > 0) + 30 * rng.standard_normal(n)
    return np.clip(np.round(x), -32768, 32767).astype("<i2")


class Delay:
    """A fake filter that delays by `d` samples and halves the level."""

    name = "fake"
    params = {}

    def __init__(self, d):
        self.d, self.lag = d, 0

    def raw(self, x):
        return (np.concatenate([np.zeros(self.d, "<i2"), x]) // 2).astype("<i2")


def test_measure_lag_and_apply_remove_the_delay():
    f = Delay(123)
    f.lag = M.measure_lag(f)
    assert f.lag == 123
    x = tone(5 * M.RATE)
    y = M.apply(f, x)
    assert len(y) == len(x)
    assert np.array_equal(y, (x // 2).astype("<i2"))


def test_residual_lag_is_zero_when_aligned_and_signed_otherwise():
    x = tone(30 * M.RATE)
    assert M.residual_lag(x, x) == 0
    shifted = np.concatenate([np.zeros(40, "<i2"), x[:-40]])
    assert M.residual_lag(x, shifted) == 40
    assert M.residual_lag(x[:100], x[:100]) is None


class Gain:
    """A stateless fake filter (halves the level, no delay)."""

    name = "gain"
    params = {}
    lag = 0

    def raw(self, x):
        return (np.asarray(x, np.int32) // 2).astype("<i2")


class Sink:
    def __init__(self):
        self.chunks = []

    def write(self, b):
        self.chunks.append(b)


def test_filter_file_in_pieces_keeps_length_and_order():
    x = tone(25 * M.RATE)
    sink = Sink()
    agg = M.filter_file(Gain(), x, sink, piece_s=4, preroll_s=1)
    y = np.frombuffer(b"".join(sink.chunks), "<i2")
    assert np.array_equal(y, (x.astype(np.int32) // 2).astype("<i2"))
    assert len(sink.chunks) == 7
    assert agg["ss_out"] < agg["ss_in"]


def test_is_target_selects_raw_wake_files_and_negatives_only():
    assert M.is_target(Path("S1/tv-c3a/raw/a@0-10.wav"))
    assert M.is_target(Path("negatives/c3-b/x-01.wav"))
    assert not M.is_target(Path("S1/pc-mic/raw/pc-mic.wav"))
    assert not M.is_target(Path("S1/tv-c3a/clips/c.wav"))
    assert not M.is_target(Path("S1/tv-c3a/raw-stt/a.wav"))
    assert not M.is_target(Path("negatives/manifest.jsonl"))


def make_dataset(root):
    (root / "S1" / "tv-c3a" / "raw").mkdir(parents=True)
    (root / "S1" / "pc-mic" / "raw").mkdir(parents=True)
    (root / "S1" / "scores").mkdir()
    (root / "negatives" / "c3-a").mkdir(parents=True)
    (root / "levels").mkdir()
    C.write_wav_new(root / "S1" / "tv-c3a" / "raw" / "a@0-10.wav", tone(3 * M.RATE, 1))
    C.write_wav_new(root / "S1" / "pc-mic" / "raw" / "pc-mic.wav", tone(3 * M.RATE, 2))
    C.write_wav_new(root / "negatives" / "c3-a" / "n-01.wav", tone(12 * M.RATE, 3))
    (root / "S1" / "tv-c3a" / "alignment.json").write_text("{}")
    (root / "S1" / "scores" / "old.json").write_text("{}")
    (root / "negatives" / "manifest.jsonl").write_text("")
    (root / "splits.jsonl").write_text("")
    (root / "levels" / "x.s16").write_bytes(b"\0\0")


def test_build_none_mirrors_links_and_copies(tmp_path):
    src, dst = tmp_path / "real", tmp_path / "derived" / "none"
    make_dataset(src)
    M.main(["build", "--src", str(src), "--dst", str(dst), "--filter", "none"])
    raw = dst / "S1" / "tv-c3a" / "raw" / "a@0-10.wav"
    assert raw.is_file() and not raw.is_symlink()
    assert np.array_equal(C.read_pcm16(raw), C.read_pcm16(src / "S1" / "tv-c3a" / "raw" / "a@0-10.wav"))
    assert (dst / "S1" / "pc-mic" / "raw" / "pc-mic.wav").is_symlink()
    assert (dst / "negatives" / "manifest.jsonl").is_symlink()
    assert (dst / "S1" / "scores").is_dir() and not any((dst / "S1" / "scores").iterdir())
    assert not (dst / "levels").exists()
    rec = json.loads((dst / "derived.json").read_text())
    assert rec["targets"] == 2 and rec["runs"][-1]["files_written"] == 2
    assert "a@0-10" not in (dst / "derived.json").read_text()       # no file names in the record
    # a second run resumes: nothing is rewritten
    M.main(["build", "--src", str(src), "--dst", str(dst), "--filter", "none"])
    rec = json.loads((dst / "derived.json").read_text())
    assert rec["runs"][-1]["files_written"] == 0 and rec["runs"][-1]["files_skipped"] == 2


def test_build_refuses_dst_inside_src(tmp_path):
    src = tmp_path / "real"
    make_dataset(src)
    with pytest.raises(SystemExit):
        M.main(["build", "--src", str(src), "--dst", str(src / "x"), "--filter", "none"])


def test_split_points_at_the_mirror(tmp_path):
    dst = tmp_path / "d"
    (dst / "negatives").mkdir(parents=True)
    sp = tmp_path / "room_split.json"
    sp.write_text(json.dumps({"negatives": "/orig/negatives", "ranges": [{"clip": "negatives/c3-a/n.wav"}]}))
    M.main(["split", "--split", str(sp), "--dst", str(dst)])
    out = json.loads((dst / "room_split.json").read_text())
    assert out["negatives"] == str(dst.resolve() / "negatives")
    assert out["ranges"] == [{"clip": "negatives/c3-a/n.wav"}]


@pytest.mark.skipif(not has_speex(), reason="pyspeex-noise not installed")
def test_speex_filter_is_aligned_and_suppresses_stationary_noise():
    f = M.SpeexFilter(2)
    f.lag = M.measure_lag(f)
    assert 0 <= f.lag < M.MAX_LAG
    rng = np.random.default_rng(5)
    noise = np.clip(np.round(300 * rng.standard_normal(20 * M.RATE)), -32768, 32767).astype("<i2")
    y = M.apply(f, noise)
    assert len(y) == len(noise)
    assert M.rms_db(y[10 * M.RATE:]) < M.rms_db(noise[10 * M.RATE:]) - 6
    x = tone(30 * M.RATE)
    assert M.residual_lag(x, M.apply(f, x)) == 0


@pytest.mark.skipif(not Path(TOOL).is_file(), reason="aec_offline not built")
@pytest.mark.parametrize("mode", ["webrtc-ns", "rnnoise"])
def test_aec_offline_filters_are_aligned_and_keep_length(mode, tmp_path):
    f = M.AecOfflineFilter(TOOL, mode, str(tmp_path))
    f.lag = M.measure_lag(f)
    x = tone(30 * M.RATE)
    y = M.apply(f, x)
    assert len(y) == len(x)
    assert abs(M.residual_lag(x, y)) <= M.LAG_TOL


class Mute:
    """A fake filter that silences everything (as a suppressor does on noise-only audio)."""

    name = "mute"
    params = {}
    lag = 0

    def raw(self, x):
        return np.zeros(len(x), "<i2")


def test_silenced_pieces_are_unverifiable_not_misaligned():
    x = tone(30 * M.RATE)
    agg = M.filter_file(Mute(), x, Sink(), piece_s=10, preroll_s=1)
    assert agg["unverifiable"] == 3 and agg["checked"] == 0 and agg["nonzero"] == 0


def test_verify_and_bench_record_aggregates(tmp_path):
    src, dst = tmp_path / "real", tmp_path / "derived" / "none"
    make_dataset(src)
    M.main(["build", "--src", str(src), "--dst", str(dst), "--filter", "none"])
    M.main(["verify", "--src", str(src), "--dst", str(dst)])
    M.main(["bench", "--src", str(src), "--filter", "none", "--hours", "1", "--dst", str(dst)])
    rec = json.loads((dst / "derived.json").read_text())
    assert rec["verify"][-1]["missing"] == 0 and rec["verify"][-1]["nonzero"] == 0
    assert rec["bench"][-1]["audio_hours"] > 0
    # a truncated derived file fails verification
    raw = dst / "S1" / "tv-c3a" / "raw" / "a@0-10.wav"
    raw.unlink()
    C.write_wav_new(raw, tone(M.RATE))
    with pytest.raises(SystemExit):
        M.main(["verify", "--src", str(src), "--dst", str(dst)])
