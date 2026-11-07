#!/usr/bin/env python3
"""Satellite front-end emulation for Kivi v2 training and evaluation audio.

Runs the satellite's own capture chain on audio through the `aec_offline` example of the
webos-voice-satellite repository (crates/capture/examples/aec_offline.rs): the daemon's file mode,
2nd-order Butterworth high-pass at 120 Hz, sonora AEC3 with noise suppression (the shipped
`[aec] enabled = true, ns = true`), fixed +24 dB gain and saturating conversion to S16. Nothing is
re-implemented here, so the emulation follows the shipped front-end exactly, except:

- the echo reference is silent (the training mixes have no separate TV reference). AEC3 then
  passes the mic through, and the TV-like interference is treated as sound the canceller does not
  remove (household voices, or residual echo). NS still acts on it.
- the mic signal is synthetic: `x` is given in "post-gain S16 units", i.e. the S16 value the chain
  would output with HPF, AEC and NS transparent. It is converted to the S24 mic value
  x * 256 / 10^(24/20), so a post-gain value above 32767 saturates exactly as on the TV.

Clips are processed as one stream per batch. Each clip is preceded by `lead` samples of its own
background (the caller supplies them), so NS has adapted to the clip's interference before the
word, as on the TV where the TV sound runs continuously. The output is aligned to the input
(the chain's latency is measured once and removed).

    from v2_frontend import Frontend
    fe = Frontend("/path/to/aec_offline")
    y = fe.process_batch([x1, x2, ...])    # float arrays in post-gain S16 units -> int16 arrays
"""
import json
import os
import subprocess
import tempfile

import numpy as np

GAIN_DB = 24.0


class Frontend:
    def __init__(self, tool, ns=True, aec=True, tmpdir=None, gap=1600):
        self.tool = tool
        self.ns = ns
        self.aec = aec
        self.tmpdir = tmpdir
        self.gap = gap  # zeros between clips in the stream (also absorbs the latency)
        self.latency = None
        self.last_metrics = None

    def _run_stream(self, s16_units):
        """One continuous stream (float, post-gain S16 units) through the chain -> int16 (unaligned)."""
        s24 = np.clip(np.round(s16_units * 256.0 / 10 ** (GAIN_DB / 20)), -(1 << 23), (1 << 23) - 1).astype(np.int32)
        with tempfile.TemporaryDirectory(dir=self.tmpdir) as d:
            mic, ref, out = (os.path.join(d, n) for n in ("mic.raw", "ref.raw", "out.wav"))
            np.repeat(s24[:, None], 2, axis=1).astype("<i4").tofile(mic)  # S24_LE in 4-byte containers, 2 ch
            np.zeros(len(s24), "<i4").tofile(ref)  # silent reference, 1 ch, 16 kHz
            cmd = [self.tool, "--mic", mic, "--ref", ref, "--out", out, "--mic-channels", "2",
                   "--ref-channels", "1", "--ref-rate", "16000", "--ref-format", "s24_le", "--skip-ms", "0",
                   "--channel", "left"]
            if self.ns:
                cmd.append("--ns")
            if not self.aec:
                cmd.append("--no-aec")
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if r.returncode != 0:
                raise RuntimeError(f"aec_offline failed ({r.returncode}): {r.stderr[-500:]}")
            try:
                self.last_metrics = json.loads(r.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                self.last_metrics = None
            y = np.fromfile(out, dtype="<i2", offset=44)
        return y

    def measure_latency(self):
        """Latency of the chain in samples: a broadband burst in a quiet floor, cross-correlated."""
        rng = np.random.default_rng(0)
        n = 16000 * 4
        x = 3.0 * rng.standard_normal(n)
        x[24000:28000] += 3000 * rng.standard_normal(4000)
        y = self._run_stream(x).astype(np.float64)
        m = min(len(x), len(y))
        xs, ys = x[20000:32000], y[:m]
        best, lag = -1, 0
        for k in range(0, 2048):
            seg = ys[20000 + k:32000 + k]
            if len(seg) < len(xs):
                break
            c = float(np.dot(seg, xs))
            if c > best:
                best, lag = c, k
        self.latency = lag
        return lag

    def process_batch(self, clips):
        """clips: list of 1-D float arrays (post-gain S16 units). Returns a list of int16 arrays of
        the same lengths, through the chain and aligned."""
        if self.latency is None:
            self.measure_latency()
        parts, bounds, pos = [], [], 0
        for c in clips:
            parts.append(np.asarray(c, np.float64))
            bounds.append((pos, pos + len(c)))
            pos += len(c)
            parts.append(np.zeros(self.gap))
            pos += self.gap
        parts.append(np.zeros(4096))
        y = self._run_stream(np.concatenate(parts))
        y = np.concatenate([y, np.zeros(max(0, pos + 4096 - len(y)), np.int16)])
        L = self.latency
        return [y[s + L:e + L].astype(np.int16) for s, e in bounds]


if __name__ == "__main__":
    import sys
    fe = Frontend(sys.argv[1])
    print("latency samples (NS on):", fe.measure_latency())
    fe2 = Frontend(sys.argv[1], ns=False)
    print("latency samples (NS off):", fe2.measure_latency())
    # speed and transparency check: a 60 s tone+noise stream
    import time
    rng = np.random.default_rng(1)
    t = np.arange(16000 * 60) / 16000
    x = 2000 * np.sin(2 * np.pi * 440 * t) + 30 * rng.standard_normal(len(t))
    t0 = time.time()
    y = fe.process_batch([x])[0]
    dt = time.time() - t0
    print(f"60 s through the chain in {dt:.2f} s; in rms {np.sqrt(np.mean(x[16000:] ** 2)):.0f} "
          f"out rms {np.sqrt(np.mean(y[16000:].astype(float) ** 2)):.0f}; metrics {fe.last_metrics}")
