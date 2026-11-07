#!/usr/bin/env python3
"""Score `kivi` and the baseline `alexa` on a real session with the server-exact feature path.

    kivi_score.py --session ~/.../datasets/real/<session id>                  # the satellite's audio
    kivi_score.py --session ... --also-pc                                     # plus pc-mic and webcam-mic

The input is the aligned full-session audio that `kivi_extract.py` stored (`timeline.wav` of the
satellite source; the raw file for pc-mic). It is streamed through `fast_eval.StreamFeatures` (the
per-chunk melspectrogram clamp and pyopen-wakeword's chunking and frame bookkeeping, see
`test_server_exact_features.py`) in pieces, then through the ONNX heads. Use the **uncalibrated**
kivi head: the thresholds are raw (the deployed, calibrated file shifts 0.98 to 0.5).

Reported per block, per model and raw threshold: recall of the cues (a cue counts when any score
inside its window is above the threshold), near-word false accepts (the same rule for the near-word
cues), and false accepts per hour on the time that is not a cue window (silence block included,
pauses between blocks excluded; 2 s refractory as in the server). `alexa` is not said in the
protocol: its recall on "kivi" is a cross-talk check, and its false accepts per hour are the baseline.

Output: a markdown table and JSON with aggregates only (counts, rates, intervals). No audio, no
text content. The score time of a window is the end of its 16-embedding window (accurate to about 0.1 s).
"""
import os

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "4")  # the PC is shared: at most 4 CPU threads

import argparse  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kivi_real_common as C  # noqa: E402
from eval_stream import detections, poisson_ci, wilson  # noqa: E402
from fast_eval import StreamFeatures, score_model  # noqa: E402

RATE = C.RATE
WORK = Path(os.environ.get("WORK", "/work/workspaces/kivi-wakeword"))
OWW_MODELS = WORK / "src/openWakeWord/openwakeword/resources/models"
DEFAULT_THRESHOLDS = "0.5,0.7,0.9,0.95,0.97,0.98,0.99"
PIECE_CHUNKS = 3750   # 300 s of audio per feature call
CTX_CHUNKS = 40       # chunks of left context re-computed per piece (an embedding needs 9)


class CappedFeatures(StreamFeatures):
    """fast_eval.StreamFeatures (same `_get_embeddings`, `_run`, chunking) with the ONNX sessions
    limited to 4 CPU threads, on the GPU when available."""

    def __init__(self, mel_rowmax, embedding, cpu=False, threads=4):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        if cpu:
            prov = ["CPUExecutionProvider"]
        else:
            import torch  # noqa: F401  loads the CUDA libraries onnxruntime-gpu needs
            prov = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        self.mel_s = ort.InferenceSession(str(mel_rowmax), sess_options=so, providers=prov)
        self.emb_s = ort.InferenceSession(str(embedding), sess_options=so, providers=prov)
        self.batch_mel = True


def stream_embeddings(F, x, piece_chunks=PIECE_CHUNKS, ctx_chunks=CTX_CHUNKS):
    """Server-exact embeddings of a long int16 signal, computed in pieces. Embedding row r belongs
    to chunk r + 9 (chunk c covers samples c*1280 .. c*1280 + 1760); a piece re-computes
    `ctx_chunks` of left context, which is exact because an embedding only needs the last 10 chunks."""
    total = (len(x) - 1760) // 1280 + 1
    if total < 10:
        return np.zeros((0, 96), np.float16)
    out = []
    for k0 in range(0, total, piece_chunks):
        k1 = min(total, k0 + piece_chunks)
        c_start = max(0, k0 - ctx_chunks)
        emb = F._get_embeddings(x[c_start * 1280:(k1 - 1) * 1280 + 1760])
        lo = max(0, k0 - c_start - 9)
        hi = k1 - c_start - 9
        if hi > lo:
            out.append(emb[lo:hi].astype(np.float16))
    return np.concatenate(out) if out else np.zeros((0, 96), np.float16)


def score_times(n):
    """Time (s) of score i: the end of the chunk of its newest embedding (row i + 15, chunk i + 24)."""
    return ((np.arange(n) + 24) * 1280 + 1760) / RATE


def load_head(path, threads=4):
    import onnx
    import onnxruntime as ort
    m = onnx.load(str(path))
    m.graph.input[0].type.tensor_type.shape.dim[0].dim_param = "N"
    m.graph.output[0].type.tensor_type.shape.dim[0].dim_param = "N"
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    return ort.InferenceSession(m.SerializeToString(), sess_options=so, providers=["CPUExecutionProvider"])


def score_audio(F, heads, x):
    """{model: per-score probabilities}, score times. `heads` is {name: onnx session}."""
    emb = stream_embeddings(F, x)
    if len(emb) < 16:
        return {n: np.zeros(0, np.float32) for n in heads}, np.zeros(0)
    probs = {n: score_model(h, emb, [(0, len(emb))])[0] for n, h in heads.items()}
    return probs, score_times(len(emb) - 15)


# ---------------------------------------------------------------------------------------------
# aggregation (pure: probabilities in, counts out)
# ---------------------------------------------------------------------------------------------


def covered_fraction(coverage, t0, t1):
    tot = max(1e-9, t1 - t0)
    return sum(max(0.0, min(t1, b) - max(t0, a)) for a, b in coverage) / tot


def covered_seconds(coverage, t0, t1):
    return sum(max(0.0, min(t1, b) - max(t0, a)) for a, b in coverage)


def rate_cell(k, n):
    lo, hi = wilson(k, n)
    return {"k": int(k), "n": int(n), "rate": round(k / n, 4) if n else None, "ci95": [round(lo, 4), round(hi, 4)]}


def analyze(session, probs, times, coverage, thresholds, pad_before=0.5, pad_after=3.5, refractory=2.0):
    """Aggregate scores of one source. Times are on the reference clock (s since the pc-mic file
    start); the session's block and cue times are converted with the pc-mic offset."""
    o = C.session_pc_offset(session)
    sess_windows = C.cue_windows(session, pad_before, pad_after)
    windows = [{**w, "t0": w["t0"] - o, "t1": w["t1"] - o} for w in sess_windows]
    segs = [(bi, lab, t0 - o, t1 - o) for bi, lab, t0, t1 in
            C.noncue_segments(session, sess_windows, min_len=0.0, max_len=1e9)]
    models = list(probs)
    blocks = {b["index"]: b for b in session["blocks"]}
    max_in = {}   # (model, window idx) -> max score in the window, or None when uncovered
    for m in models:
        for i, w in enumerate(windows):
            if covered_fraction(coverage, w["t0"], w["t1"]) < 0.9:
                max_in[(m, i)] = None
                continue
            sel = (times >= w["t0"]) & (times <= w["t1"])
            max_in[(m, i)] = float(probs[m][sel].max()) if sel.any() else 0.0
    dets = {(m, t): np.asarray(detections(probs[m], times, t, refractory)) for m in models for t in thresholds}
    out_blocks = []
    for bi in sorted(blocks):
        b = blocks[bi]
        idx = [i for i, w in enumerate(windows) if w["block"] == bi]
        entry = {"index": bi, "name": b["name"], "kind": b["kind"], "condition": C.condition_string(b),
                 "cues": len(idx), "uncovered_cues": sum(max_in[(models[0], i)] is None for i in idx) if models else 0}
        entry["cue_hits"] = {m: {str(t): rate_cell(sum(1 for i in idx if max_in[(m, i)] is not None and max_in[(m, i)] > t),
                                                    sum(1 for i in idx if max_in[(m, i)] is not None))
                                 for t in thresholds} for m in models}
        if b["kind"] == "near":
            words = {}
            for i in idx:
                words.setdefault(windows[i]["word"].lower(), []).append(i)
            entry["by_word"] = {wd: {m: {str(t): rate_cell(sum(1 for i in ii if max_in[(m, i)] is not None and max_in[(m, i)] > t),
                                                          sum(1 for i in ii if max_in[(m, i)] is not None))
                                         for t in thresholds} for m in models} for wd, ii in sorted(words.items())}
        bsegs = [s for s in segs if s[0] == bi]
        hours = sum(covered_seconds(coverage, s[2], s[3]) for s in bsegs) / 3600
        entry["noncue_hours"] = round(hours, 4)
        entry["false_accepts"] = {}
        for m in models:
            entry["false_accepts"][m] = {}
            for t in thresholds:
                d = dets[(m, t)]
                k = int(sum(1 for x in d for s in bsegs if s[2] <= x <= s[3] and covered_seconds(coverage, x, x + 1e-3) > 0))
                lo, hi = poisson_ci(k, hours) if hours > 0 else (0.0, float("nan"))
                entry["false_accepts"][m][str(t)] = {"k": k, "hours": round(hours, 4),
                                                      "per_hour": round(k / hours, 3) if hours else None,
                                                      "ci95": [round(lo, 3), round(hi, 3)] if hours else None}
        out_blocks.append(entry)
    return {"thresholds": thresholds, "refractory_s": refractory, "pad_s": [pad_before, pad_after],
            "blocks": out_blocks, "overall": overall(out_blocks, models, thresholds)}


def overall(blocks, models, thresholds):
    """Totals over blocks: kivi recall per condition, near-words in total and per word, false
    accepts per hour over all non-cue time."""
    res = {"kivi_by_condition": {}, "near_total": {}, "near_by_word": {}, "false_accepts": {}}

    def pool(selected, key):
        return {m: {str(t): rate_cell(sum(b[key][m][str(t)]["k"] for b in selected),
                                      sum(b[key][m][str(t)]["n"] for b in selected)) for t in thresholds} for m in models}

    for cond in sorted({b["condition"] for b in blocks if b["kind"] == "kivi"}):
        res["kivi_by_condition"][cond] = pool([b for b in blocks if b["kind"] == "kivi" and b["condition"] == cond], "cue_hits")
    kb = [b for b in blocks if b["kind"] == "kivi"]
    if kb:
        res["kivi_by_condition"]["ALL"] = pool(kb, "cue_hits")
    nb = [b for b in blocks if b["kind"] == "near"]
    if nb:
        res["near_total"] = pool(nb, "cue_hits")
        for wd in sorted({w for b in nb for w in b.get("by_word", {})}):
            res["near_by_word"][wd] = {m: {str(t): rate_cell(sum(b["by_word"][wd][m][str(t)]["k"] for b in nb if wd in b["by_word"]),
                                                              sum(b["by_word"][wd][m][str(t)]["n"] for b in nb if wd in b["by_word"]))
                                           for t in thresholds} for m in models}
    hours = sum(b["noncue_hours"] for b in blocks)
    for m in models:
        res["false_accepts"][m] = {}
        for t in thresholds:
            k = sum(b["false_accepts"][m][str(t)]["k"] for b in blocks)
            lo, hi = poisson_ci(k, hours) if hours > 0 else (0.0, float("nan"))
            res["false_accepts"][m][str(t)] = {"k": k, "hours": round(hours, 4),
                                               "per_hour": round(k / hours, 3) if hours else None,
                                               "ci95": [round(lo, 3), round(hi, 3)] if hours else None}
    return res


# ---------------------------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------------------------


def _cell(c):
    return "n/a" if not c["n"] else f"{c['k']}/{c['n']} ({100 * c['rate']:.0f}%)"


def to_markdown(rep):
    thr = rep["thresholds"]
    head = "| " + " | ".join(["", "model"] + [f"> {t}" for t in thr]) + " |\n|" + " --- |" * (len(thr) + 2) + "\n"
    L = [f"## {rep['source']} - session {rep['session']} (speaker {rep.get('speaker') or '?'}, "
         f"device {rep.get('device')}, split {rep.get('split')})", ""]
    for m, p in rep["models"].items():
        L.append(f"- model `{m}`: {p}")
    L += ["", f"Raw thresholds (kivi: the uncalibrated head, so server 0.5 = raw 0.98). Cue window "
          f"-{rep['pad_s'][0]} s .. +{rep['pad_s'][1]} s. Detections use a {rep['refractory_s']} s refractory time.", ""]
    ov = rep["overall"]
    models = list(rep["models"])

    def block_table(title, rows):
        L.append(f"### {title}\n")
        L.append(head.rstrip("\n"))
        for name, per_model in rows:
            for m in models:
                L.append("| " + " | ".join([name, m] + [_cell(per_model[m][str(t)]) for t in thr]) + " |")
        L.append("")

    kb = [(b["name"] + f" ({b['condition']})", b["cue_hits"]) for b in rep["blocks"] if b["kind"] == "kivi"]
    if kb:
        pooled = [(f"{c} (pooled)", v) for c, v in ov["kivi_by_condition"].items() if c != "ALL"]
        block_table("Recall on \"kivi\" cues per block", kb + pooled + [("ALL", ov["kivi_by_condition"]["ALL"])])
    if ov["near_total"]:
        block_table("Near-word false accepts (cues that scored above the threshold)",
                    [("all near words", ov["near_total"])] + [(w, v) for w, v in ov["near_by_word"].items()])
    L.append("### False accepts per hour on the non-cue time\n")
    L.append("| block | hours | model | " + " | ".join(f"> {t}" for t in thr) + " |")
    L.append("|" + " --- |" * (len(thr) + 3))

    def fa_cell(c):
        return "n/a" if not c["hours"] else f"{c['k']} ({c['per_hour']:.2f}/h)"

    for b in rep["blocks"]:
        if b["noncue_hours"] <= 0:
            continue
        for m in models:
            L.append("| " + " | ".join([b["name"], f"{b['noncue_hours']:.3f}", m] + [fa_cell(b["false_accepts"][m][str(t)]) for t in thr]) + " |")
    for m in models:
        hrs = next(iter(ov["false_accepts"][m].values()))["hours"]
        L.append("| **all** | " + " | ".join([f"{hrs:.3f}", m] + [fa_cell(ov["false_accepts"][m][str(t)]) for t in thr]) + " |")
    unc = sum(b["uncovered_cues"] for b in rep["blocks"])
    if unc:
        L += ["", f"{unc} cue(s) were not covered by this source's audio and are left out of the counts."]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------------


def load_source(sdir, label):
    """(int16 timeline on the reference clock, coverage intervals) of a dataset source."""
    base = Path(sdir) / label
    als = sorted(base.glob("alignment*.json"), key=lambda p: p.stat().st_mtime)
    if not als:
        raise SystemExit(f"{base}: no alignment.json (run kivi_extract.py first)")
    al = json.loads(als[-1].read_text(encoding="utf-8"))
    return C.read_pcm16(base / al["timeline"]), al["coverage_s"]


def score_session(a, F=None, heads=None):
    sdir = Path(a.session).expanduser()
    session = json.loads((sdir / "session.json").read_text(encoding="utf-8"))
    thr = [float(t) for t in a.thresholds.split(",")]
    models = dict(m.split("=", 1) for m in a.model) if a.model else {
        "kivi": str(WORK / "final/kivi_uncalibrated.onnx"), "alexa": str(OWW_MODELS / "alexa_v0.1.onnx")}
    if F is None:
        mel = a.mel_rowmax or str(WORK / "emb_s_melrow.onnx")
        emb = a.embedding_model or str(OWW_MODELS / "embedding_model.onnx")
        F = CappedFeatures(mel, emb, cpu=a.cpu)
    heads = heads or {n: load_head(p) for n, p in models.items()}
    sources = [a.source or session.get("tv_source") or C.source_label(session["device"])]
    if a.also_pc:
        sources += list(C.PC_SOURCES)
    reports = []
    for label in sources:
        x, cov = load_source(sdir, label)
        probs, times = score_audio(F, heads, x)
        rep = analyze(session, probs, times, cov, thr, a.pad_before, a.pad_after, a.refractory)
        rep.update(session=session["session_id"], speaker=session.get("speaker", ""), device=session["device"],
                   split=session.get("split", ""), source=label, models={n: Path(p).name for n, p in models.items()},
                   audio_hours=round(len(x) / RATE / 3600, 4))
        reports.append(rep)
    return reports


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--session", required=True, help="the session directory inside the dataset")
    ap.add_argument("--source", help="satellite source label (default: the session's)")
    ap.add_argument("--also-pc", action="store_true", help="also score pc-mic and webcam-mic, for comparison")
    ap.add_argument("--model", action="append", metavar="NAME=head.onnx",
                    help="default: kivi (uncalibrated head) and alexa (openWakeWord package)")
    ap.add_argument("--mel-rowmax", help="melspectrogram ONNX rewritten by stream_mel.py")
    ap.add_argument("--embedding-model")
    ap.add_argument("--thresholds", default=DEFAULT_THRESHOLDS)
    ap.add_argument("--pad-before", type=float, default=0.5)
    ap.add_argument("--pad-after", type=float, default=3.5)
    ap.add_argument("--refractory", type=float, default=2.0)
    ap.add_argument("--cpu", action="store_true", help="no GPU")
    ap.add_argument("--json", help="write the JSON here (default: <session>/scores/)")
    ap.add_argument("--md", help="write the markdown here (default: <session>/scores/)")
    ap.add_argument("--no-save", action="store_true")
    a = ap.parse_args(argv)
    reports = score_session(a)
    md = "\n".join(to_markdown(r) for r in reports)
    print(md)
    if not a.no_save:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        d = Path(a.json).parent if a.json else C.private_mkdir(Path(a.session).expanduser() / "scores", Path(a.session).expanduser())
        jp = Path(a.json) if a.json else d / f"score-{stamp}.json"
        mp = Path(a.md) if a.md else d / f"score-{stamp}.md"
        C.write_json_new(jp, reports)
        with C.create_exclusive(mp) as f:
            f.write(md.encode("utf-8"))
        print(f"saved {jp} and {mp}")


if __name__ == "__main__":
    main()
