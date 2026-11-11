#!/usr/bin/env python3
"""Kivi v6: room audio (satellite wake-stage recordings) as server-exact embeddings, scored by many heads.

Why: v6 needs the same 146 h of v5-era room audio three times (threshold on the EVEN-day `mine-v5era` part, hard
negatives mined from it, false wakes on the ODD-day `test2-v5era` part) and for several heads (v4, v5, v6, the
ablation). The melspectrogram and embedding models are the expensive part and identical for every head, so they run
once per file; heads are tiny and run on the stored embeddings.

Framing is the server's (pyopen-wakeword, wyoming-openwakeword): each piece is a stream from its sample 0; step c
uses the 1760 samples [1280 c, 1280 c + 1760), the melspectrogram is clamped per step (the row-max ONNX model of
stream_mel.py), and the embedding of step c covers the 76 mel frames that start at step c - 9. The head at step c
sees the embeddings of steps c - 15 .. c. This equals the server's state after its 8 s zero autofill has left the
window, i.e. from step 24 on (the first ~2 s of a piece are not scored; the server would score them against zeros).
The step time is the end of its audio, (1280 c + 1760) / 16000 s after the piece start. Detections: probability
above the raw threshold, 2 s refractory, as wyoming-openwakeword's handler.

    v6_room.py embed --manifest DATASET/negatives-v5era/manifest.jsonl --out-dir v6/room_emb [--skip-list F]
    v6_room.py score --index v6/room_emb/index.json --model v5=a.onnx ... --thresholds 0.9,0.95 \
        --part test2-v5era --json out.json
    v6_room.py curve --index ... --model v6=a.onnx --part mine-v5era --json out.json   # events per threshold
    v6_room.py mine --index ... --model v5=a.onnx --part mine-v5era --above 0.3 --out hard.npy

Aggregates only: times, scores and counts, never audio. Embedding files are private (mode 600, dir 700).
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RATE = 16000
CHUNK = 1280
WIN = 1760
FIRST_STEP = 24            # first step whose 16 embeddings all come from the piece's own audio
REFRACTORY_S = 2.0
TZ = ZoneInfo("Europe/Madrid")
EVENING = (18, 24)
DEFAULT_ONNX = ("/work/workspaces/kivi-wakeword/emb_s_melrow.onnx,"
                "/work/workspaces/kivi-wakeword/src/openWakeWord/openwakeword/resources/models/embedding_model.onnx")


def n_steps(n_samples):
    return 0 if n_samples < WIN else (n_samples - WIN) // CHUNK + 1


def step_time(c):
    """End of step c's audio, seconds from the piece start."""
    return (CHUNK * np.asarray(c) + WIN) / RATE


def embed_block(F, x, c0, c1, batch=4096):
    """Embeddings of steps [c0, c1) of the stream x (int16, sample 0 = stream start); steps < 9 get zeros."""
    lo = max(0, c0 - 9)
    idx = np.arange(lo, c1)[:, None] * CHUNK + np.arange(WIN)[None, :]
    chunks = x[idx].astype(np.float32)
    mels = np.concatenate([F._run(None, np.ascontiguousarray(chunks[k:k + batch]), "mel_n")
                           for k in range(0, len(chunks), batch)])
    mels = (mels.reshape(len(chunks), -1, 32) / 10 + 2).astype(np.float32)   # (steps, 8, 32)
    assert mels.shape[1] == 8
    flat = mels.reshape(-1, 32)
    out = np.zeros((c1 - c0, 96), np.float32)
    cs = np.arange(max(c0, 9), c1)
    if len(cs):
        starts = 8 * (cs - 9 - lo)
        win = np.stack([flat[s:s + 76] for s in starts])[..., None]
        emb = np.concatenate([F._run(None, np.ascontiguousarray(win[k:k + batch]), "emb_n").reshape(-1, 96)
                              for k in range(0, len(win), batch)])
        out[cs - c0] = emb
    return out


def embed_stream(F, x, block=16384):
    """All step embeddings of x (int16) as float16 (steps, 96)."""
    n = n_steps(len(x))
    out = np.zeros((n, 96), np.float16)
    for c0 in range(0, n, block):
        c1 = min(n, c0 + block)
        lo = max(0, c0 - 9)
        seg = x[lo * CHUNK:(c1 - 1) * CHUNK + WIN]
        out[c0:c1] = embed_block(F, seg, c0 - lo, c1 - lo).astype(np.float16)
    return out


def _utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def cmd_embed(a):
    import soundfile as sf
    import torch  # noqa: F401  CUDA libraries for onnxruntime-gpu
    from fast_eval import StreamFeatures
    F = StreamFeatures(*a.stream_onnx.split(","))
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    idx_path = out / "index.json"
    index = json.loads(idx_path.read_text()) if idx_path.exists() else {"pieces": []}
    done = {p["clip"] for p in index["pieces"]}
    skip = set(Path(a.skip_list).read_text().split()) if a.skip_list else set()
    for mf in a.manifest:
        root = Path(mf).parent.parent
        rows = [json.loads(line) for line in open(mf)]
        for r in rows:
            if r.get("superseded") or r["clip"] in done:
                continue
            if r["clip"] in skip:
                print("skip (listed)", r["device"], flush=True)
                continue
            p = root / r["clip"]
            x, sr = sf.read(p, dtype="int16", always_2d=True)
            assert sr == RATE
            x = x[:, 0]
            e = embed_stream(F, x)
            dst = out / r["device"] / (Path(r["clip"]).stem + ".npy")
            dst.parent.mkdir(exist_ok=True)
            os.chmod(dst.parent, 0o700)
            np.save(dst, e)
            os.chmod(dst, 0o600)
            part = r.get("split") or "unassigned"
            if part == "unassigned":
                part = "old"
            index["pieces"].append({"clip": r["clip"], "emb": str(dst), "device": r["device"], "part": part,
                                    "start_utc": r["start_utc"], "samples": int(len(x)), "steps": int(len(e))})
            idx_path.write_text(json.dumps(index, indent=1))
            os.chmod(idx_path, 0o600)
            print(f"{r['device']} {part} {len(x) / RATE / 3600:.2f} h", flush=True)


# ------------------------------------------------------------------------------------------------
# heads
# ------------------------------------------------------------------------------------------------


class Head:
    """An exported openWakeWord `dnn` head (ONNX with batch 1, as exported) rebuilt in torch from its weights, so it
    scores any batch on the GPU. Same graph as `train_simple.py` trains and exports."""

    def __init__(self, path):
        import onnx
        import torch
        from onnx import numpy_helper
        from openwakeword.train import Model
        w0 = {i.name: torch.tensor(numpy_helper.to_array(i)) for i in onnx.load(str(path)).graph.initializer}
        layer = int(w0["layer1.weight"].shape[0])
        nblk = len({k.split(".")[1] for k in w0 if k.startswith("blocks.")})
        m = Model(n_classes=1, input_shape=(16, 96), model_type="dnn", layer_dim=layer, n_blocks=nblk)
        sd = m.model.state_dict()
        assert set(sd) <= set(w0), f"{path}: missing {set(sd) - set(w0)}"
        m.model.load_state_dict({k: w0[k] for k in sd})
        self.torch = torch
        self.dev = "cuda" if torch.cuda.is_available() else "cpu"
        self.net = m.model.to(self.dev).eval()

    def __call__(self, w, batch=65536):
        if not len(w):
            return np.zeros(0, np.float32)
        out = []
        with self.torch.no_grad():
            for k in range(0, len(w), batch):
                x = self.torch.from_numpy(np.ascontiguousarray(w[k:k + batch], np.float32)).to(self.dev)
                out.append(self.net(x).reshape(-1).float().cpu().numpy())
        return np.concatenate(out)


def window_view(e):
    """(steps - 15, 16, 96) view: window j ends at step j + 15."""
    from numpy.lib.stride_tricks import sliding_window_view
    return sliding_window_view(e, 16, axis=0).transpose(0, 2, 1)


def piece_probs(head, e, block=200000):
    """Probabilities per step from FIRST_STEP on; returns (steps array, probs)."""
    v = window_view(e)                          # window j -> step j + 15
    j0 = FIRST_STEP - 15
    ps = []
    for k in range(j0, len(v), block):
        ps.append(head(np.asarray(v[k:k + block], np.float32)))
    p = np.concatenate(ps) if ps else np.zeros(0, np.float32)
    return np.arange(j0, j0 + len(p)) + 15, p


def detections(times, p, thr, refractory=REFRACTORY_S):
    det, last = [], -1e9
    for i in np.flatnonzero(p > thr):
        if times[i] - last >= refractory:
            det.append(i)
            last = times[i]
    return det


def poisson_ci(k, t):
    from scipy.stats import chi2
    lo = 0.0 if k == 0 else chi2.ppf(0.025, 2 * k) / 2
    hi = chi2.ppf(0.975, 2 * k + 2) / 2
    return lo / t, hi / t


def evening_hours(start_utc, samples):
    """{local date: hours of this piece inside 18-24 local} and the local dates of the piece's evening seconds."""
    t0 = _utc(start_utc).timestamp()
    t1 = t0 + samples / RATE
    out = {}
    day = datetime.fromtimestamp(t0, TZ).date()
    while True:
        e0 = datetime(day.year, day.month, day.day, EVENING[0], tzinfo=TZ).timestamp()
        e1 = e0 + (EVENING[1] - EVENING[0]) * 3600
        if e0 >= t1:
            break
        ov = min(t1, e1) - max(t0, e0)
        if ov > 0:
            out[day.isoformat()] = ov / 3600
        day = datetime.fromordinal(day.toordinal() + 1).date()
    return out


def is_evening(start_utc, t_s):
    lt = datetime.fromtimestamp(_utc(start_utc).timestamp() + t_s, TZ)
    return EVENING[0] <= lt.hour < EVENING[1], lt.date().isoformat()


def local_date(start_utc):
    return datetime.fromtimestamp(_utc(start_utc).timestamp(), TZ).date().isoformat()


def load_index(a):
    """Pieces of the parts in --part, optionally only those whose start lies on a --local-date (Europe/Madrid; the
    copies are cut at local midnight, so a piece belongs to one local date)."""
    idx = json.loads(Path(a.index).read_text())["pieces"]
    parts = set(a.part.split(","))
    dates = set(a.local_date.split(",")) if getattr(a, "local_date", None) else None
    return [p for p in idx if p["part"] in parts and (dates is None or local_date(p["start_utc"]) in dates)]


def heads_from(a):
    return {s.split("=", 1)[0]: Head(s.split("=", 1)[1]) for s in a.model}


def cmd_score(a):
    pieces = load_index(a)
    heads = heads_from(a)
    thrs = sorted({float(t) for t in a.thresholds.split(",")})
    acc = {}          # (model, device) -> dict
    ev_hours = {}     # device -> {date: hours}
    hours = {}
    for pc in pieces:
        e = np.load(pc["emb"], mmap_mode="r")
        dev = pc["device"]
        hours[dev] = hours.get(dev, 0.0) + pc["samples"] / RATE / 3600
        for d, h in evening_hours(pc["start_utc"], pc["samples"]).items():
            ev_hours.setdefault(dev, {}).setdefault(d, 0.0)
            ev_hours[dev][d] += h
        for m, hd in heads.items():
            steps, p = piece_probs(hd, e)
            times = step_time(steps)
            for t in thrs:
                det = detections(times, p, t)
                c = acc.setdefault((m, dev, t), {"k": 0, "k_evening": 0, "by_evening": {}})
                c["k"] += len(det)
                for i in det:
                    ev, d = is_evening(pc["start_utc"], float(times[i]))
                    if ev:
                        c["k_evening"] += 1
                        c["by_evening"][d] = c["by_evening"].get(d, 0) + 1
    out = {"part": a.part, "models": {s.split("=", 1)[0]: s.split("=", 1)[1] for s in a.model},
           "thresholds": thrs, "hours": {d: round(h, 3) for d, h in hours.items()},
           "evening_hours": {d: {k: round(v, 3) for k, v in x.items()} for d, x in ev_hours.items()}, "cells": []}
    devs = sorted(hours)
    for m in heads:
        for t in thrs:
            rows = []
            for dev in devs + ["pooled"]:
                ds = devs if dev == "pooled" else [dev]
                k = sum(acc[(m, d, t)]["k"] for d in ds)
                ke = sum(acc[(m, d, t)]["k_evening"] for d in ds)
                h = sum(hours[d] for d in ds)
                eh = sum(sum(ev_hours.get(d, {}).values()) for d in ds)
                # evenings = device-evenings with at least 3 h of the 6 h recorded
                n_ev = sum(1 for d in ds for v in ev_hours.get(d, {}).values() if v >= 3.0)
                full_eh = sum(v for d in ds for v in ev_hours.get(d, {}).values())
                lo, hi = poisson_ci(k, h)
                elo, ehi = poisson_ci(ke, eh / 6.0) if eh > 0 else (0, 0)
                rows.append({"model": m, "threshold": t, "scope": dev, "hours": round(h, 3), "k": int(k),
                             "per_hour": round(k / h, 4), "ci95_per_hour": [round(lo, 4), round(hi, 4)],
                             "per_8h": round(8 * k / h, 3),
                             "evening_hours": round(eh, 3), "k_evening": int(ke),
                             "per_evening": round(ke / (eh / 6.0), 3) if eh else None,
                             "ci95_per_evening": [round(elo, 3), round(ehi, 3)],
                             "device_evenings_ge3h": n_ev, "evening_equiv": round(full_eh / 6.0, 3),
                             "by_evening": {d: acc[(m, d, t)]["by_evening"] for d in ds}})
            out["cells"].extend(rows)
    for r in out["cells"]:
        print(f"{r['model']:8s} {r['threshold']:.4f} {r['scope']:6s} {r['k']:4d} in {r['hours']:.2f} h = "
              f"{r['per_hour']:.3f}/h {r['ci95_per_hour']}  evening {r['k_evening']} in {r['evening_equiv']} ev = "
              f"{r['per_evening']} {r['ci95_per_evening']}", flush=True)
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1))


def cmd_curve(a):
    """Event counts (2 s refractory) on a threshold grid, total and evening, per device and pooled."""
    pieces = load_index(a)
    heads = heads_from(a)
    grid = np.round(np.concatenate([np.arange(0.5, 0.9, 0.02), np.arange(0.9, 0.99951, 0.0005)]), 5)
    res = {}
    hours, ev_h = {}, {}
    for pc in pieces:
        e = np.load(pc["emb"], mmap_mode="r")
        dev = pc["device"]
        hours[dev] = hours.get(dev, 0.0) + pc["samples"] / RATE / 3600
        ev_h[dev] = ev_h.get(dev, 0.0) + sum(evening_hours(pc["start_utc"], pc["samples"]).values())
        for m, hd in heads.items():
            steps, p = piece_probs(hd, e)
            times = step_time(steps)
            hi_idx = np.flatnonzero(p > grid[0])
            tt, pp = times[hi_idx], p[hi_idx]
            for t in grid:
                sel = pp > t
                det, last = [], -1e9
                for ti in tt[sel]:
                    if ti - last >= REFRACTORY_S:
                        det.append(ti)
                        last = ti
                ke = sum(1 for ti in det if is_evening(pc["start_utc"], float(ti))[0])
                c = res.setdefault(m, {}).setdefault(dev, {}).setdefault(float(t), [0, 0])
                c[0] += len(det)
                c[1] += ke
    out = {"part": a.part, "hours": hours, "evening_hours": ev_h, "grid": grid.tolist(), "curves": res}
    Path(a.json).write_text(json.dumps(out))
    print(json.dumps({"hours": hours, "evening_hours": ev_h}))


def cmd_mine(a):
    """Hard negatives: the windows (16, 96) of every step where the mining head scores above --above, plus --around
    steps either side. With --split-evening, windows whose step lies in 18-24 Europe/Madrid go to <out>_evening.npy and
    the rest to <out>_day.npy (so training can weight evenings up). --summary writes counts per part, device, local
    date and evening (steps above the floor, distinct events with the 2 s refractory, windows kept)."""
    pieces = load_index(a)
    hd = Head(a.model[0].split("=", 1)[1])
    keep = {"day": [], "evening": []}
    summ = {}
    for pc in pieces:
        e = np.load(pc["emb"], mmap_mode="r")
        steps, p = piece_probs(hd, e)
        hit = np.flatnonzero(p > a.above)
        if not len(hit):
            continue
        times = step_time(steps)
        sel = set()
        for i in hit:
            for d in range(-a.around, a.around + 1):
                if 0 <= i + d < len(p):
                    sel.add(i + d)
        sel = np.array(sorted(sel))
        v = window_view(e)
        ev = np.array([is_evening(pc["start_utc"], float(times[i]))[0] for i in sel])
        X = np.asarray(v[steps[sel] - 15], np.float16)
        keep["evening"].append(X[ev])
        keep["day"].append(X[~ev])
        events = detections(times, p, a.above)
        for i in events:
            evn, day = is_evening(pc["start_utc"], float(times[i]))
            k = f"{pc['part']}|{pc['device']}|{day}|{'evening' if evn else 'day'}"
            c = summ.setdefault(k, {"events": 0, "steps": 0, "windows": 0})
            c["events"] += 1
        for i, evn in zip(sel, ev):
            day = is_evening(pc["start_utc"], float(times[i]))[1]
            k = f"{pc['part']}|{pc['device']}|{day}|{'evening' if evn else 'day'}"
            c = summ.setdefault(k, {"events": 0, "steps": 0, "windows": 0})
            c["windows"] += 1
            c["steps"] += int(p[i] > a.above)
    outs = {}
    if a.split_evening:
        for k in ("day", "evening"):
            outs[a.out.replace(".npy", f"_{k}.npy")] = keep[k]
    else:
        outs[a.out] = keep["day"] + keep["evening"]
    for f, parts in outs.items():
        X = np.concatenate(parts) if parts else np.zeros((0, 16, 96), np.float16)
        np.save(f, X)
        os.chmod(f, 0o600)
        print(f, X.shape, flush=True)
    if a.summary:
        Path(a.summary).write_text(json.dumps(dict(sorted(summ.items())), indent=1))
    for k, c in sorted(summ.items()):
        print(k, c, flush=True)


def cmd_windows(a):
    """Every `--stride`-th window (16, 96) of the parts as one float16 npy (room negatives for training)."""
    pieces = load_index(a)
    excl = json.loads(Path(a.exclude_json).read_text()) if a.exclude_json else {}
    tot = sum(max(0, (p["steps"] - FIRST_STEP) // a.stride + 1) for p in pieces if p["steps"] > FIRST_STEP)
    X = np.lib.format.open_memmap(a.out + ".tmp.npy", mode="w+", dtype=np.float16, shape=(tot, 16, 96))
    k = dropped = 0
    for pc in pieces:
        if pc["steps"] <= FIRST_STEP:
            continue
        e = np.load(pc["emb"], mmap_mode="r")
        v = window_view(e)
        idx = np.arange(FIRST_STEP - 15, len(v), a.stride)
        iv = excl.get(pc["device"], [])
        if iv:
            t = _utc(pc["start_utc"]).timestamp() + step_time(idx + 15)
            keep = np.ones(len(idx), bool)
            for t0, t1 in iv:
                keep &= ~((t >= t0) & (t <= t1))
            dropped += int((~keep).sum())
            idx = idx[keep]
        X[k:k + len(idx)] = v[idx]
        k += len(idx)
    X.flush()
    np.save(a.out, X[:k])
    del X
    os.remove(a.out + ".tmp.npy")
    os.chmod(a.out, 0o600)
    print(a.out, (k, 16, 96), "of", tot, "dropped by --exclude-json", dropped)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("embed")
    p.add_argument("--manifest", action="append", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--skip-list", help="file with clip paths (manifest `clip` values) to leave out")
    p.add_argument("--stream-onnx", default=DEFAULT_ONNX)
    p = sp.add_parser("windows")
    p.add_argument("--index", required=True)
    p.add_argument("--part", required=True)
    p.add_argument("--local-date")
    p.add_argument("--stride", type=int, default=8)
    p.add_argument("--exclude-json", help="{device: [[epoch0, epoch1], ...]}: windows whose step falls inside are left out")
    p.add_argument("--out", required=True)
    for name in ("score", "curve", "mine"):
        p = sp.add_parser(name)
        p.add_argument("--index", required=True)
        p.add_argument("--part", required=True, help="comma list: mine-v5era, test2-v5era, old")
        p.add_argument("--local-date", help="comma list of Europe/Madrid dates (YYYY-MM-DD) to keep")
        p.add_argument("--model", action="append", required=True, metavar="NAME=head.onnx")
        if name == "score":
            p.add_argument("--thresholds", required=True)
        if name in ("score", "curve"):
            p.add_argument("--json")
        if name == "mine":
            p.add_argument("--above", type=float, default=0.3)
            p.add_argument("--around", type=int, default=1)
            p.add_argument("--out", required=True)
            p.add_argument("--split-evening", action="store_true")
            p.add_argument("--summary")
    a = ap.parse_args()
    {"embed": cmd_embed, "score": cmd_score, "curve": cmd_curve, "mine": cmd_mine, "windows": cmd_windows}[a.cmd](a)


if __name__ == "__main__":
    main()
