#!/usr/bin/env python3
"""Fast model selection: cache openWakeWord embeddings once, then score many ONNX models.

`embed` computes the embedding sequence (one 96-d frame per 80 ms) of long WAV files or of clips
(each padded with 1 s of silence before and after) with openWakeWord's ONNX feature models.
`score` runs one or more ONNX wake-word models over every 16-frame window and reports recall (clip
max above threshold) and false accepts per hour (2 s refractory) at several thresholds.
Numbers are for choosing between training runs; the reported results come from eval_stream.py,
which uses the server's TFLite runtime.

    fast_eval.py embed --out emb/mls_dev.npz eval_audio/mls_es_dev
    fast_eval.py embed --clips eval_clips/eval_kivi --manifest eval_clips/eval_kivi.manifest.csv --out emb/kivi.npz
    fast_eval.py score --model a=run_a.onnx --model b=run_b.onnx --pos emb/kivi.npz --neg emb/mls_dev.npz
"""
import argparse
import csv
import glob
import os

import numpy as np

RATE = 16000


class TFLiteFeatures:
    """The server's melspectrogram and embedding TFLite models (from pyopen-wakeword), vectorised
    but chunked exactly like the streaming server."""

    def __init__(self, models_dir):
        import tensorflow as tf
        self.tf = tf
        self.mel = tf.lite.Interpreter(model_path=os.path.join(models_dir, "melspectrogram.tflite"), num_threads=1)
        self.emb = tf.lite.Interpreter(model_path=os.path.join(models_dir, "embedding_model.tflite"), num_threads=1)
        self.mel_n = self.emb_n = None
        self.batch_mel = False

    def _run(self, interp, x, key):
        if getattr(self, key) != x.shape:
            interp.resize_tensor_input(0, list(x.shape))
            interp.allocate_tensors()
            setattr(self, key, x.shape)
        interp.set_tensor(interp.get_input_details()[0]["index"], x)
        interp.invoke()
        return interp.get_tensor(interp.get_output_details()[0]["index"])

    def _get_embeddings(self, x):
        """Exactly as pyopen-wakeword streams: melspectrogram of 1760-sample chunks every 1280
        samples (the model's log scaling depends on the chunk), 8 frames each, and one embedding per
        chunk over the last 76 mel frames."""
        x = x.astype(np.float32)
        n = (len(x) - 1760) // 1280 + 1
        idx = np.arange(n)[:, None] * 1280 + np.arange(1760)[None, :]
        mels = []
        step = 4096 if self.batch_mel else 1  # TFLite: one chunk per call (its scaling is per call)
        for k in range(0, n, step):
            out = self._run(getattr(self, "mel", None), np.ascontiguousarray(x[idx[k:k + step]]), "mel_n")
            mels.append(out.reshape(out.shape[0], -1, 32))
        mels = np.concatenate(mels) / 10 + 2          # (chunks, frames per chunk, 32)
        fpc = mels.shape[1]
        mels = mels.reshape(-1, 32)
        # replay pyopen-wakeword's bookkeeping: +fpc mel frames per chunk, one embedding per 8 frames
        starts, new, total = [], 0, 0
        for _ in range(n):
            total += fpc
            new = min(970, new + fpc)
            while new >= 76:
                starts.append(total - new)
                new = max(0, new - 8)
        ends = np.array(starts) + 76
        win = np.stack([mels[e - 76:e] for e in ends])[..., None].astype(np.float32)
        embs = []
        for k in range(0, len(win), 4096):
            embs.append(self._run(getattr(self, "emb", None), np.ascontiguousarray(win[k:k + 4096]), "emb_n").reshape(-1, 96))
        return np.concatenate(embs)


class StreamFeatures(TFLiteFeatures):
    """Same chunking and bookkeeping as the server, with ONNX models on the GPU: the melspectrogram
    model rewritten by stream_mel.py (per-row max) and openWakeWord's embedding model."""

    def __init__(self, mel_rowmax, embedding):
        import torch  # noqa: F401  loads the CUDA libraries onnxruntime-gpu needs
        import onnxruntime as ort
        prov = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        self.mel_s = ort.InferenceSession(mel_rowmax, providers=prov)
        self.emb_s = ort.InferenceSession(embedding, providers=prov)
        self.batch_mel = True

    def _run(self, interp, x, key):
        sess = self.mel_s if key == "mel_n" else self.emb_s
        return sess.run(None, {sess.get_inputs()[0].name: x})[0]


def server_exact_tail(F, audio, n_windows=16):
    """Server-exact features of the last `n_windows` embeddings of each equal-length clip in
    `audio` (n, samples), as the server would compute them at the end of that audio: 1760-sample
    chunks every 1280 samples, per-chunk melspectrogram clamp, one embedding per chunk (ending 4 mel
    frames before the chunk end, as pyopen-wakeword's bookkeeping does). Returns (n, n_windows, 96) float32."""
    n, L = audio.shape
    nch = (L - 1760) // 1280 + 1
    assert nch - 9 >= n_windows, "clip too short for the requested windows"
    idx = np.arange(nch)[:, None] * 1280 + np.arange(1760)[None, :]
    chunks = audio.astype(np.float32)[:, idx].reshape(-1, 1760)
    mels = np.concatenate([F._run(None, np.ascontiguousarray(chunks[k:k + 8192]), "mel_n")
                           for k in range(0, len(chunks), 8192)])
    mels = (mels.reshape(n, nch * 8, 32) / 10 + 2).astype(np.float32)
    # pyopen-wakeword keeps 80 new mel frames after each chunk and takes the window starting at the
    # oldest of them, so each embedding ends 4 frames before the chunk's last frame
    ends = 8 * (np.arange(nch - n_windows, nch) + 1) - 4
    win = np.stack([mels[:, e - 76:e] for e in ends], axis=1)  # (n, n_windows, 76, 32)
    win = win.reshape(-1, 76, 32, 1)
    emb = np.concatenate([F._run(None, np.ascontiguousarray(win[k:k + 8192]), "emb_n").reshape(-1, 96)
                          for k in range(0, len(win), 8192)])
    return emb.reshape(n, n_windows, 96)


def embed_audio(F, x):
    x = np.concatenate([x, np.zeros(RATE // 2, np.int16)])
    return F._get_embeddings(x).astype(np.float16)  # (frames, 96)


def cmd_embed(a):
    import soundfile as sf
    if a.stream_onnx:
        mel, emb = a.stream_onnx.split(",")
        F = StreamFeatures(mel, emb)
        a.tflite_models = True  # same 8 s lead as the server
    elif a.tflite_models:
        F = TFLiteFeatures(a.tflite_models)
    else:
        import torch  # noqa: F401  loads the CUDA libraries onnxruntime-gpu needs
        from openwakeword.utils import AudioFeatures
        F = AudioFeatures(device="gpu")
    seqs, names = [], []
    lead = np.zeros(8 * RATE if a.tflite_models else 0, np.int16)  # the server's 8 s "autofill"
    bg = sorted(glob.glob(os.path.join(a.bg_dir, "*.wav"))) if a.bg_dir else []
    rng = np.random.default_rng(a.seed)
    if a.clips:
        rows = list(csv.DictReader(open(a.manifest, encoding="utf-8")))
        audio = []
        for r in rows:
            x, _ = sf.read(os.path.join(a.clips, r["file"]), dtype="int16")
            z = np.zeros(RATE, np.int16)
            y = np.concatenate([z, x, z]).astype(np.float32)
            if bg:  # same mixing as eval_stream.py clips: SNR against the clip's power
                b, _ = sf.read(bg[rng.integers(len(bg))], dtype="int16")
                b = b.astype(np.float32)
                k = rng.integers(0, max(1, len(b) - len(y)))
                b = np.resize(b[k:k + len(y)], len(y))
                y = y + b * np.sqrt((np.mean(x.astype(np.float32) ** 2) + 1e-9) /
                                    (np.mean(b ** 2) + 1e-9) / 10 ** (a.snr_db / 10))
                if np.max(np.abs(y)) > 32000:
                    y *= 32000 / np.max(np.abs(y))
            audio.append(y.astype(np.int16))
            names.append(r["file"] + "|" + r.get("voice", "") + "|" + r.get("text", ""))
        if getattr(F, "batch_mel", False):
            # One long signal: each clip after 2.5 s of zeros, its 1 s lead starting on a chunk
            # boundary as in a fresh server stream (after the 8 s autofill), and each
            # segment padded to whole chunks. One embedding per chunk (8 mel frames per chunk), so the
            # embeddings split cleanly per segment; 2.5 s covers the model's whole context.
            segs, bounds, pos = [], [], 0
            for y in audio:
                seg = np.concatenate([np.zeros(39680, np.int16), y])  # 31 whole chunks
                seg = np.concatenate([seg, np.zeros(-len(seg) % 1280 + 1280, np.int16)])
                segs.append(seg)
                bounds.append((pos // 1280, (pos + len(seg)) // 1280))
                pos += len(seg)
            emb = F._get_embeddings(np.concatenate(segs + [np.zeros(1760, np.int16)]))
            for c0, c1 in bounds:  # embedding e ends at chunk e + 9
                seqs.append(emb[max(0, c0 - 9):max(0, c1 - 9)].astype(np.float16))
        else:
            for y in audio:
                seqs.append(embed_audio(F, np.concatenate([lead, y])))
    else:
        for d in a.inputs:
            for f in sorted(glob.glob(os.path.join(d, "*.wav"))):
                x, _ = sf.read(f, dtype="int16")
                for k in range(0, len(x), 60 * RATE):  # 1 min pieces
                    if len(x) - k < 2 * RATE:
                        continue
                    seqs.append(embed_audio(F, x[k:k + 60 * RATE + 12400]))
                    names.append(f"{os.path.basename(f)}@{k // RATE}")
    # Provenance: only server-exact features may be used for reported numbers (fast_report.py checks).
    kind = "server-exact" if (a.stream_onnx or a.tflite_models) else "batch"
    np.savez(a.out, names=np.array(names), lengths=np.array([len(s) for s in seqs]),
             data=np.concatenate(seqs), features=np.array(kind))
    print(a.out, len(seqs), sum(len(s) for s in seqs))


def windows(z):
    out, off = [], 0
    for n in z["lengths"]:
        out.append((off, n))
        off += n
    return out


def score_model(sess, data, spans, batch=8192):
    """Max-per-span and per-frame scores of 16-frame windows."""
    name = sess.get_inputs()[0].name
    per = []
    for off, n in spans:
        if n < 16:
            per.append(np.zeros(0, np.float32))
            continue
        idx = np.arange(n - 15)[:, None] + np.arange(16)[None, :] + off
        s = []
        for k in range(0, len(idx), batch):
            s.append(sess.run(None, {name: data[idx[k:k + batch]].astype(np.float32)})[0].ravel())
        per.append(np.concatenate(s))
    return per


def cmd_score(a):
    import onnxruntime as ort
    thr = [float(t) for t in a.thresholds.split(",")]
    pos = np.load(a.pos) if a.pos else None
    negs = [np.load(n) for n in a.neg or []]
    for spec in a.model:
        label, path = spec.split("=", 1)
        import onnx
        m = onnx.load(path)  # exported with batch 1; make the batch dimension dynamic
        m.graph.input[0].type.tensor_type.shape.dim[0].dim_param = "N"
        m.graph.output[0].type.tensor_type.shape.dim[0].dim_param = "N"
        so = ort.SessionOptions()
        so.intra_op_num_threads = 4
        sess = ort.InferenceSession(m.SerializeToString(), sess_options=so, providers=["CPUExecutionProvider"])
        line = [label]
        if pos is not None:
            mx = np.array([p.max() if len(p) else 0 for p in score_model(sess, pos["data"], windows(pos))])
            line += [f"recall@{t}={np.mean(mx > t):.3f}" for t in thr]
        for z, nf in zip(negs, a.neg or []):
            per = score_model(sess, z["data"], windows(z))
            hours = sum(len(p) for p in per) * 0.08 / 3600
            for t in thr:
                k = 0
                for p in per:
                    last = -1e9
                    for i in np.nonzero(p > t)[0]:
                        if (i - last) * 0.08 >= 2.0:
                            k += 1
                            last = i
                line.append(f"{os.path.basename(nf)}@{t}={k}/{hours:.1f}h")
        print("  ".join(line), flush=True)


def cmd_mine(a):
    """Hard-negative mining: the top-scoring 16-frame windows of speech without the wake word."""
    import onnx
    import onnxruntime as ort
    m = onnx.load(a.model)
    m.graph.input[0].type.tensor_type.shape.dim[0].dim_param = "N"
    m.graph.output[0].type.tensor_type.shape.dim[0].dim_param = "N"
    so = ort.SessionOptions()
    so.intra_op_num_threads = 4
    sess = ort.InferenceSession(m.SerializeToString(), sess_options=so, providers=["CPUExecutionProvider"])
    z = np.load(a.emb)
    data, spans = z["data"], windows(z)
    per = score_model(sess, data, spans)
    cand = []
    for (off, n), p in zip(spans, per):
        for i in np.nonzero(p > a.min_score)[0]:
            cand.append((float(p[i]), off + int(i)))
    cand.sort(reverse=True)
    picked, taken = [], np.zeros(len(data) + 16, bool)
    for sc, start in cand:  # at most one window per 0.4 s
        if taken[max(0, start - 4):start + 5].any():
            continue
        taken[start] = True
        picked.append(start)
        if len(picked) >= a.max:
            break
    X = np.stack([data[s:s + 16] for s in picked]).astype(np.float16)
    np.save(a.out, X)
    print(a.out, X.shape, "from", len(cand), "windows above", a.min_score)


def cmd_windows(a):
    """Turn an embedding cache into training windows (N, 16, 96), one every `stride` frames."""
    z = np.load(a.emb)
    data = z["data"]  # load once (npz members are re-read on every access)
    out = []
    for off, n in windows(z):
        for i in range(0, n - 15, a.stride):
            out.append(data[off + i:off + i + 16])
    X = np.stack(out).astype(np.float16)
    np.save(a.out, X)
    print(a.out, X.shape)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("embed")
    p.add_argument("inputs", nargs="*")
    p.add_argument("--clips")
    p.add_argument("--manifest")
    p.add_argument("--out", required=True)
    p.add_argument("--bg-dir")
    p.add_argument("--snr-db", type=float, default=10.0)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--stream-onnx", help="MEL_ROWMAX.onnx,EMBEDDING.onnx: server-exact features, batched")
    p.add_argument("--tflite-models", help="dir with the server's melspectrogram/embedding .tflite "
                                            "(pyopen_wakeword/models); run in a venv with tensorflow")
    p = sp.add_parser("score")
    p.add_argument("--model", action="append", required=True)
    p.add_argument("--pos")
    p.add_argument("--neg", action="append")
    p.add_argument("--thresholds", default="0.1,0.3,0.5,0.7")
    p = sp.add_parser("mine")
    p.add_argument("--model", required=True)
    p.add_argument("--emb", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--min-score", type=float, default=0.1)
    p.add_argument("--max", type=int, default=60000)
    p = sp.add_parser("windows")
    p.add_argument("--emb", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--stride", type=int, default=16)
    a = ap.parse_args()
    {"embed": cmd_embed, "score": cmd_score, "mine": cmd_mine, "windows": cmd_windows}[a.cmd](a)


if __name__ == "__main__":
    main()
