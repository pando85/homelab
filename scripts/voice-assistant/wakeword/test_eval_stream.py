"""Tests of eval_stream.stream (server framing of wake-file tails). Synthetic noise only, the builtin alexa model, no
household audio.

    cd scripts/voice-assistant/wakeword && OMP_NUM_THREADS=4 \
        /work/workspaces/kivi-wakeword/.venv/bin/python -m pytest test_eval_stream.py -q
"""
import os
import sys
from pathlib import Path

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "4")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_stream as E  # noqa: E402

RATE, CHUNK = E.RATE, E.CHUNK


def noise(n, seed=0, amp=1500):
    r = np.random.default_rng(seed)
    env = 1.0 + np.sin(np.arange(n) / 4000.0) ** 2
    return (r.standard_normal(n) * amp * env).astype(np.int16)


@pytest.fixture(scope="module")
def runtime():
    return E.load_models(["alexa=builtin"])


def test_wake_samples_is_exact_for_the_server_lengths():
    for n in (1280 * 100 + 640, 1280 * 100 + 800, 160 * 33, 160 * 9999):
        assert E.wake_samples(round(n / RATE, 2)) == n
    assert E.wake_samples(13.12) == 209920 and E.wake_samples(0.0) == 0


def test_grid_skip_arithmetic():
    assert E.grid_skip(1000, None) == 0
    assert E.grid_skip(1000, 1000) == 0                          # the audio is the whole stream
    assert E.grid_skip(6 * RATE, 6 * RATE + 640) == 640          # tail starts at 640: next grid point is 1280
    assert E.grid_skip(100, 1280 * 7 + 100) == 0                 # starts on the grid
    assert E.grid_skip(6 * RATE, 1280 * 90 + 800) == (-(1280 * 90 + 800 - 6 * RATE)) % CHUNK
    for n_total in range(96000, 96000 + 3000, 160):
        s = E.grid_skip(96000, n_total)
        assert 0 <= s < CHUNK and (n_total - 96000 + s) % CHUNK == 0


def test_remainder_is_fed_and_times_align(runtime):
    feats, models = runtime
    x = noise(RATE * 12)                                         # multiple of 1280 samples? 192000 = 150 * 1280: yes
    assert len(x) % CHUNK == 0
    p0, t0 = E.stream(feats, models, x)
    assert len(t0) == len(x) // CHUNK and np.allclose(t0, (np.arange(len(t0)) + 1) * CHUNK / RATE)
    for rem in (640, 800, 160, 1279):
        y = noise(RATE * 12 + rem)
        p, t = E.stream(feats, models, y)
        assert len(t) == len(y) // CHUNK + (1 if rem >= 480 else 0), rem     # the step needs 480 samples past the chunk
        assert np.allclose(t[:len(y) // CHUNK], (np.arange(len(y) // CHUNK) + 1) * CHUNK / RATE)
        assert t[-1] <= len(y) / RATE and np.all(np.diff(t) > 0) and len(p["alexa"]) == len(t)


def test_old_and_new_agree_on_common_steps(runtime):
    """Whole chunks give the scores of before; the new version only adds the step(s) of the remainder."""
    feats, models = runtime
    y = noise(RATE * 20 + 800, seed=3)
    pn, tn = E.stream(feats, models, y)
    pw, tw = E.stream(feats, models, y[:len(y) // CHUNK * CHUNK])     # no remainder: exactly the old behaviour
    k = len(tw)
    assert len(tn) == k + 1 and np.array_equal(pn["alexa"][:k], pw["alexa"]) and np.allclose(tn[:k], tw)


def test_tail_with_grid_phase_reproduces_the_full_stream(runtime):
    feats, models = runtime
    for n in (CHUNK * 150 + 640, CHUNK * 150 + 800):
        full = noise(n, seed=n)
        pf, tf = E.stream(feats, models, full)
        tail = full[-6 * RATE:]
        skip = E.grid_skip(len(tail), n)
        assert skip > 0
        pt, tt = E.stream(feats, models, tail, n_total=n)
        # the tail step ending at tt is the full-stream step ending at tt + (n - len(tail))
        off = (n - len(tail)) / RATE
        m = 20
        assert np.allclose(tt[-m:] + off, tf[-m:], atol=1e-6)
        assert np.allclose(pt["alexa"][-m:], pf["alexa"][-m:], atol=1e-4)
        # tail-anchored replay (n_total=None) sits on another grid: different step times
        _, t_plain = E.stream(feats, models, tail)
        assert not np.allclose(t_plain[-m:] + off, tf[-m:])


def test_tail_shorter_than_skip_and_empty(runtime):
    feats, models = runtime
    p, t = E.stream(feats, models, noise(300), n_total=10_000_000)
    assert len(t) == 0 and len(p["alexa"]) == 0
    p, t = E.stream(feats, models, np.zeros(0, dtype=np.int16))
    assert len(t) == 0
