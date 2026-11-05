#!/usr/bin/env python3
"""Prepare the downloaded datasets for openWakeWord training and evaluation (PC only).

Subcommands (all paths are arguments; nothing is written into the repository):

  acav-trim FILE            Fix the header of a partial (HTTP range) download of
                            openwakeword_features_ACAV100M_2000_hrs_16bit.npy so np.load(mmap) accepts it.
  rirs OUT                  Fetch the MIT environmental impulse responses (16 kHz) from Hugging Face.
  background PARQUET.. --out DIR [--max N] [--seg-s 10]
                            Decode audio rows of Hugging Face parquet files (AudioSet, MLS, ...) into
                            16 kHz mono WAV clips, used as background noise by the augmentation.
  features PARQUET.. --out FILE.npy [--max-hours H]
                            Spanish speech (MLS parquet) -> openWakeWord features shaped (N, 16, 96), the
                            same layout as the ACAV100M negatives, for training as extra negative data.
  evalaudio PARQUET.. --out DIR [--minutes 10] [--name PREFIX]
                            Concatenate utterances into ~10 min 16 kHz WAV streams for the false-accept
                            evaluation, with a CSV of the source rows (ids and transcripts).
"""
import argparse
import csv
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

import numpy as np

RATE = 16000


def decode(b):
    import soundfile as sf
    from scipy.signal import resample_poly
    from math import gcd
    x, sr = sf.read(io.BytesIO(b), dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    if sr != RATE:
        g = gcd(sr, RATE)
        x = resample_poly(x, RATE // g, sr // g).astype(np.float32)
    return x


def rows(paths, columns=None):
    import pyarrow.parquet as pq
    for p in paths:
        f = pq.ParquetFile(p)
        for g in range(f.num_row_groups):
            for r in f.read_row_group(g, columns=columns).to_pylist():
                yield r


def acav_trim(path):
    with open(path, "r+b") as f:
        magic = f.read(8)
        assert magic[:6] == b"\x93NUMPY" and magic[6] == 1, "expect npy v1"
        hl = int.from_bytes(f.read(2), "little")
        header = f.read(hl).decode("latin1")
        d = eval(header, {"__builtins__": {}})  # npy header is a Python literal dict
        rowsize = int(np.prod(d["shape"][1:])) * np.dtype(d["descr"]).itemsize
        size = os.path.getsize(path)
        n = (size - 10 - hl) // rowsize
        new = "{'descr': '%s', 'fortran_order': False, 'shape': (%d, %d, %d), }" % (
            d["descr"], n, d["shape"][1], d["shape"][2])
        new = new.ljust(hl - 1) + "\n"
        assert len(new) == hl
        f.seek(10)
        f.write(new.encode("latin1"))
        f.truncate(10 + hl + n * rowsize)
    hours = n * 1.28 / 3600  # one row = 16 embedding frames = 1.28 s
    print(f"{path}: {n} rows of {d['shape'][1:]}, {d['descr']} (about {hours:.0f} h of the original {d['shape'][0]} rows)")


def fetch_rirs(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    api = "https://huggingface.co/api/datasets/davidscripka/MIT_environmental_impulse_responses/tree/main/16khz"
    files = [x["path"] for x in json.load(urllib.request.urlopen(api, timeout=60)) if x["path"].endswith(".wav")]

    def get(p):
        dst = out / Path(p).name
        if dst.exists():
            return
        url = "https://huggingface.co/datasets/davidscripka/MIT_environmental_impulse_responses/resolve/main/" + p
        for _ in range(3):
            try:
                data = urllib.request.urlopen(url, timeout=60).read()
                break
            except OSError:
                continue
        assert data[:4] == b"RIFF", p
        dst.write_bytes(data)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(get, files))
    print(f"{len(files)} RIRs in {out}")


def background(paths, out, max_n, seg_s):
    import soundfile as sf
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for r in rows(paths, ["audio"]):
        x = decode(r["audio"]["bytes"])
        seg = int(seg_s * RATE)
        for k in range(0, max(1, len(x) - seg // 2), seg):
            y = x[k:k + seg]
            if len(y) < RATE:
                continue
            peak = float(np.max(np.abs(y)))
            if peak < 1e-4:
                continue
            sf.write(out / f"bg_{n:06d}.wav", (y / max(peak, 1e-9) * 0.9 * 32767).astype(np.int16), RATE)
            n += 1
            if max_n and n >= max_n:
                print(f"{n} clips in {out}")
                return
    print(f"{n} clips in {out}")


def features(paths, out, max_hours):
    """Embeddings of 1.28 s windows (16 frames), stepped by 16 frames, as float16."""
    import torch  # noqa: F401  loads the CUDA libraries onnxruntime-gpu needs
    import openwakeword.utils
    F = openwakeword.utils.AudioFeatures(device="gpu", ncpu=8)
    chunks, total_s = [], 0.0
    batch = []

    def flush():
        if not batch:
            return
        L = max(len(b) for b in batch)
        X = np.zeros((len(batch), L), dtype=np.int16)
        for i, b in enumerate(batch):
            X[i, :len(b)] = b
        emb = F.embed_clips(X, batch_size=64)  # (n, frames, 96)
        for i, b in enumerate(batch):
            nfr = min(emb.shape[1], (len(b) - 12400) // 1280 + 1)  # frames covering real audio only
            e = emb[i, :max(nfr, 0)]
            for k in range(0, e.shape[0] - 16 + 1, 16):
                chunks.append(e[k:k + 16].astype(np.float16))
        batch.clear()

    for r in rows(paths, ["audio"]):
        x = decode(r["audio"]["bytes"])
        batch.append((np.clip(x, -1, 1) * 32767).astype(np.int16))
        total_s += len(x) / RATE
        if len(batch) == 64:
            flush()
            print(f"\r{total_s / 3600:.1f} h, {len(chunks)} windows", end="", flush=True)
        if max_hours and total_s >= max_hours * 3600:
            break
    flush()
    arr = np.stack(chunks)
    np.save(out, arr)
    print(f"\n{out}: {arr.shape} from {total_s / 3600:.1f} h")


def evalaudio(paths, out, minutes, name):
    import soundfile as sf
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    meta = []
    buf, k, t = [], 0, 0.0
    gap = np.zeros(int(0.3 * RATE), dtype=np.float32)
    cols = None
    for r in rows(paths):
        x = decode(r["audio"]["bytes"])
        text = r.get("transcript") or r.get("normalized_text") or ""
        rid = r.get("id") or r.get("audio_id") or ""
        meta.append({"file": f"{name}_{k:03d}.wav", "start_s": round(t, 2), "dur_s": round(len(x) / RATE, 2),
                     "id": rid, "speaker_id": r.get("speaker_id", ""), "text": text})
        buf += [x, gap]
        t += (len(x) + len(gap)) / RATE
        if t >= minutes * 60:
            y = np.concatenate(buf)
            sf.write(out / f"{name}_{k:03d}.wav", (np.clip(y, -1, 1) * 32767).astype(np.int16), RATE)
            buf, t, k = [], 0.0, k + 1
    if buf:
        y = np.concatenate(buf)
        sf.write(out / f"{name}_{k:03d}.wav", (np.clip(y, -1, 1) * 32767).astype(np.int16), RATE)
        k += 1
    with open(out / f"{name}_sources.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(meta[0]))
        w.writeheader()
        w.writerows(meta)
    print(f"{k} files, {sum(m['dur_s'] for m in meta) / 3600:.2f} h of speech in {out}")


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("acav-trim"); p.add_argument("file")
    p = sp.add_parser("rirs"); p.add_argument("out")
    p = sp.add_parser("background"); p.add_argument("parquet", nargs="+"); p.add_argument("--out", required=True)
    p.add_argument("--max", type=int, default=0); p.add_argument("--seg-s", type=float, default=10.0)
    p = sp.add_parser("features"); p.add_argument("parquet", nargs="+"); p.add_argument("--out", required=True)
    p.add_argument("--max-hours", type=float, default=0)
    p = sp.add_parser("evalaudio"); p.add_argument("parquet", nargs="+"); p.add_argument("--out", required=True)
    p.add_argument("--minutes", type=float, default=10); p.add_argument("--name", required=True)
    a = ap.parse_args()
    if a.cmd == "acav-trim":
        acav_trim(a.file)
    elif a.cmd == "rirs":
        fetch_rirs(a.out)
    elif a.cmd == "background":
        background(a.parquet, a.out, a.max, a.seg_s)
    elif a.cmd == "features":
        features(a.parquet, a.out, a.max_hours)
    elif a.cmd == "evalaudio":
        evalaudio(a.parquet, a.out, a.minutes, a.name)


if __name__ == "__main__":
    sys.exit(main())
