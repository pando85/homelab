#!/usr/bin/env python3
"""Kivi v3: the room-audio negatives (kivi_negatives.py) split by time, as features and as a test set.

`split`: per TV, the pieces of the negatives manifest (superseded rows and `*.part` files are never
read) are ordered by their start time and cut by cumulative duration into train (the first
`--train` share, default 50 %), sel (threshold selection, the next 25 %) and test (the latest 25 %).
A piece that straddles a boundary is cut at the sample. Writes the split (piece, sample range, part)
as JSON next to the features (private), and prints hours per TV and part.

`embed`: server-exact embeddings (fast_eval.StreamFeatures: the server's chunking with a per-chunk
melspectrogram clamp) of the train and sel parts, per TV, in 1 min pieces (as fast_eval.py embed),
tagged server-exact. The audio is the satellite's own wake-stage audio, so there is no front-end
emulation, no level change and no added floor.

`score`: the test part (or another part) through the server's runtime itself (pyopen-wakeword, TFLite
C API, `eval_stream.stream`), each range in pieces of at most `--piece-min` minutes streamed from
its first sample like a new run; detections with the 2 s refractory at each threshold, per TV and
pooled, exact Poisson 95 % intervals. Aggregates only (no piece names in the output).

    v3_room.py split --negatives DATASET/negatives --out v3/room_split.json
    v3_room.py embed --split v3/room_split.json --parts train,sel --stream-onnx MEL,EMB --out-dir v3/feat
    v3_room.py score --split v3/room_split.json --part test --model v1=a.tflite --model v3=b.tflite \
        --thresholds 0.5,0.9,0.95 --json out.json
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kivi_real_common as C  # noqa: E402

RATE = 16000


def _t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def cmd_split(a):
    root = Path(a.negatives)
    rows = [r for r in C.read_manifest(root) if not r.get("superseded")]
    shares = (a.train, a.train + a.sel)
    out = {"negatives": str(root), "shares": {"train": a.train, "sel": a.sel, "test": round(1 - a.train - a.sel, 3)},
           "ranges": []}
    summary = {}
    for dev in sorted({r["device"] for r in rows}):
        pieces = sorted((r for r in rows if r["device"] == dev), key=lambda r: _t(r["start_utc"]))
        lens = []
        for r in pieces:
            p = root.parent / r["clip"]
            assert not str(p).endswith(".part")
            lens.append(len(C.read_pcm16(p)))
        tot = sum(lens)
        cuts = [int(round(s * tot)) for s in shares]
        pos = 0
        for r, n in zip(pieces, lens):
            bounds = [0] + [c - pos for c in cuts if pos < c < pos + n] + [n]
            for s0, s1 in zip(bounds[:-1], bounds[1:]):
                g = pos + s0
                part = "train" if g < cuts[0] else "sel" if g < cuts[1] else "test"
                out["ranges"].append({"device": dev, "clip": r["clip"], "s0": int(s0), "s1": int(s1), "part": part,
                                      "start_utc": r["start_utc"]})
                summary.setdefault(dev, {}).setdefault(part, 0.0)
                summary[dev][part] += (s1 - s0) / RATE / 3600
            pos += n
    out["hours"] = {d: {p: round(h, 3) for p, h in v.items()} for d, v in summary.items()}
    # the parts must be ordered in time within each TV (test latest)
    for dev in summary:
        rs = [r for r in out["ranges"] if r["device"] == dev]
        order = ["train", "sel", "test"]
        assert [order.index(r["part"]) for r in rs] == sorted(order.index(r["part"]) for r in rs)
    Path(a.out).write_text(json.dumps(out, indent=1))
    os.chmod(a.out, 0o600)
    print(json.dumps(out["hours"]))


def cmd_embed(a):
    import torch  # noqa: F401
    from fast_eval import StreamFeatures, embed_audio
    sp = json.loads(Path(a.split).read_text())
    root = Path(sp["negatives"]).parent
    F = StreamFeatures(*a.stream_onnx.split(","))
    for part in a.parts.split(","):
        for dev in sorted({r["device"] for r in sp["ranges"]}):
            seqs, names = [], []
            for r in sp["ranges"]:
                if r["device"] != dev or r["part"] != part:
                    continue
                x = C.read_pcm16(root / r["clip"])[r["s0"]:r["s1"]]
                for k in range(0, len(x), 60 * RATE):
                    if len(x) - k < 2 * RATE:
                        continue
                    seqs.append(embed_audio(F, x[k:k + 60 * RATE + 12400]))
                    names.append(f"{dev}:{part}@{len(names)}")
            out = os.path.join(a.out_dir, f"room_{dev}_{part}.npz")
            np.savez(out, names=np.array(names), lengths=np.array([len(s) for s in seqs]),
                     data=np.concatenate(seqs), features=np.array("server-exact"),
                     meta=np.array("room negatives, satellite wake-stage audio as recorded"))
            print(out, len(seqs), sum(len(s) for s in seqs), flush=True)


_W = {}


def _score_worker(args):
    path, s0, s1, specs = args
    from eval_stream import load_models, stream
    if "m" not in _W:
        _W["m"] = load_models(specs)
    feats, models = _W["m"]
    x = C.read_pcm16(path)[s0:s1]
    probs, times = stream(feats, models, x)
    return (s1 - s0) / RATE, probs, times


def cmd_score(a):
    import multiprocessing as mp
    from eval_stream import detections, poisson_ci
    sp = json.loads(Path(a.split).read_text())
    root = Path(sp["negatives"]).parent
    # unique: a repeated threshold used to be counted once per repetition (v3 final passed 0.95 and 0.98 twice)
    thr = sorted({float(t) for t in a.thresholds.split(",")})
    names = [s.split("=", 1)[0] for s in a.model]
    tasks, devs = [], []
    step = int(a.piece_min * 60 * RATE)
    for r in sp["ranges"]:
        if r["part"] != a.part:
            continue
        for s in range(r["s0"], r["s1"], step):
            e = min(r["s1"], s + step)
            if e - s < 2 * RATE:
                continue
            tasks.append((str(root / r["clip"]), s, e, a.model))
            devs.append(r["device"])
    res = {d: {"hours": 0.0, "k": {m: {t: 0 for t in thr} for m in names}} for d in set(devs)}
    maxes = {m: [] for m in names}
    with mp.get_context("spawn").Pool(a.jobs) as pool:
        for n, (dev, (dur, probs, times)) in enumerate(zip(devs, pool.imap(_score_worker, tasks))):
            res[dev]["hours"] += dur / 3600
            for m in names:
                for t in thr:
                    res[dev]["k"][m][t] += len(detections(probs[m], times, t, a.refractory))
            if n % 10 == 9:
                print(f"{n + 1}/{len(tasks)}", file=sys.stderr, flush=True)
    out = {"part": a.part, "models": {s.split("=", 1)[0]: s.split("=", 1)[1] for s in a.model}, "thresholds": thr,
           "refractory_s": a.refractory, "pieces": len(tasks), "scopes": {}}
    scopes = {d: (v["hours"], v["k"]) for d, v in res.items()}
    scopes["pooled"] = (sum(v["hours"] for v in res.values()),
                        {m: {t: sum(v["k"][m][t] for v in res.values()) for t in thr} for m in names})
    for sc, (h, ks) in sorted(scopes.items()):
        out["scopes"][sc] = {"hours": round(h, 3), "false_accepts": {}}
        for m in names:
            cells = {}
            for t in thr:
                k = ks[m][t]
                lo, hi = poisson_ci(k, h)
                cells[str(t)] = {"k": int(k), "per_hour": round(k / h, 3), "ci95": [round(lo, 3), round(hi, 3)]}
            out["scopes"][sc]["false_accepts"][m] = cells
        print(sc, f"{h:.2f} h", {m: {t: ks[m][t] for t in thr} for m in names}, flush=True)
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("split")
    p.add_argument("--negatives", default=os.path.join(C.DEFAULT_DATASET_ROOT, "negatives"))
    p.add_argument("--train", type=float, default=0.5)
    p.add_argument("--sel", type=float, default=0.25)
    p.add_argument("--out", required=True)
    p = sp.add_parser("embed")
    p.add_argument("--split", required=True)
    p.add_argument("--parts", default="train,sel")
    p.add_argument("--stream-onnx", required=True)
    p.add_argument("--out-dir", required=True)
    p = sp.add_parser("score")
    p.add_argument("--split", required=True)
    p.add_argument("--part", default="test")
    p.add_argument("--model", action="append", required=True, metavar="NAME=model.tflite")
    p.add_argument("--thresholds", default="0.5,0.9,0.95,0.97,0.98,0.99")
    p.add_argument("--refractory", type=float, default=2.0)
    p.add_argument("--piece-min", type=float, default=15.0)
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--json")
    a = ap.parse_args()
    {"split": cmd_split, "embed": cmd_embed, "score": cmd_score}[a.cmd](a)


if __name__ == "__main__":
    main()
