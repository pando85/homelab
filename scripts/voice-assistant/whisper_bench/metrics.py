"""Text and latency metrics for the Whisper GPU benchmark (pure functions, no server, no GPU).

Normalisation reuses decider_poc/numbers_es.py so that "veinte", "20" and "20 por ciento" /
"20 %" compare equal. Latency statistics are percentiles; paired comparisons use an exact
McNemar test (exact match) and a paired bootstrap over clips (latency).
"""
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "decider_poc"))
import numbers_es as ne  # noqa: E402


def _fmt_number(v):
    return str(int(v)) if float(v).is_integer() else ("%g" % v)


def normalize(text):
    """Lower case, no accents or punctuation, '%' as 'por ciento', degree sign as 'grados',
    every number (digits or words, 'y medio' included) replaced by its digits. Returns a list of
    tokens."""
    t = (text or "").replace("%", " por ciento ").replace("°", " grados ").replace("º", " grados ")
    tokens = ne.fold(t).split()
    for start, end, val in sorted(ne.tokens_to_numbers(tokens), reverse=True):
        tokens[start:end] = [_fmt_number(val)]
    return tokens


def edit_distance(a, b):
    """Levenshtein distance between two sequences."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def word_errors(ref, hyp):
    """(edits, reference words) after normalisation."""
    r, h = normalize(ref), normalize(hyp)
    return edit_distance(r, h), len(r)


def char_errors(ref, hyp):
    """(edits, reference characters) on the normalised text with single spaces."""
    r, h = " ".join(normalize(ref)), " ".join(normalize(hyp))
    return edit_distance(r, h), len(r)


def exact_match(ref, hyp):
    return normalize(ref) == normalize(hyp)


def percentile(values, q):
    """q in [0, 100], linear interpolation (numpy's default)."""
    xs = sorted(values)
    if not xs:
        return math.nan
    pos = (len(xs) - 1) * q / 100.0
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def wilson(k, n, z=1.96):
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def mcnemar_exact(base_ok, other_ok):
    """Paired booleans -> (fixed, broken, two-sided exact p). fixed = other right, base wrong."""
    fixed = sum(1 for a, b in zip(base_ok, other_ok) if b and not a)
    broken = sum(1 for a, b in zip(base_ok, other_ok) if a and not b)
    n = fixed + broken
    if n == 0:
        return fixed, broken, 1.0
    k = min(fixed, broken)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return fixed, broken, min(1.0, 2 * p)


def paired_bootstrap(base, other, stat="median", n_boot=10000, seed=1234):
    """Paired difference of a statistic (other minus base) with a 95 % percentile bootstrap CI over
    clips. Returns (diff, lo, hi). stat: 'median', 'mean' or 'p90'."""
    import numpy as np
    b, o = np.asarray(base, dtype=float), np.asarray(other, dtype=float)
    assert b.shape == o.shape and b.size > 0
    f = {"median": lambda x: np.median(x, axis=-1), "mean": lambda x: x.mean(axis=-1),
         "p90": lambda x: np.percentile(x, 90, axis=-1)}[stat]
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, b.size, size=(n_boot, b.size))
    diffs = f(o[idx]) - f(b[idx])
    return float(f(o) - f(b)), float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))
