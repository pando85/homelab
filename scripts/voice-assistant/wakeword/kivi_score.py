#!/usr/bin/env python3
"""Score `kivi` and the baseline `alexa` on a real session with the server's own runtime.

    kivi_score.py --session ~/.../datasets/real/<session id>                  # the satellite's audio
    kivi_score.py --session ... --also-pc                                     # plus pc-mic and webcam-mic

Each raw audio file that `kivi_extract.py` stored is streamed from its sample 0 through
`eval_stream.stream`, which is pyopen-wakeword (the library inside wyoming-openwakeword 2.x, TFLite C
API) fed in 1280-sample chunks as 16-bit PCM, with the library's own 8 s startup autofill. No
re-implemented features, no float16 cache. (The 2.1.0 handler does not re-chunk: it resets on AudioStart and passes each
converted AudioChunk straight to process_streaming; only HA's chunk size is unconfirmed.) The score times are mapped to the
desk-mic clock through the stored alignment. Files whose alignment was not trusted are left out
unless `--include-unaligned`. Use the **uncalibrated** kivi model: thresholds are raw (the deployed,
calibrated file shifts 0.98 to 0.5).

Reported per block, per model and raw threshold: recall of the cues (a cue counts when any score
inside its window is above the threshold), near-word false accepts (the same rule for the near-word
cues), and false accepts per hour on the time that is not a cue window (silence block included,
pauses between blocks excluded; 2 s refractory as in the server). Cues that no aligned audio covers
are `lost` and stay in the denominators (counted as misses, listed separately). A closed wake file that
ends inside a cue window is a server detection of the live wake word: it is counted as
`run_end_cues`, and file ends outside cue windows as `server_run_ends`. `alexa` is not said in the
protocol: its recall on "kivi" is a cross-talk check, and its false accepts per hour are the baseline.

Output: a markdown table and JSON with aggregates only (counts, rates, intervals). No audio, no
text content. Score times are the end of an 80 ms step.
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
from eval_stream import detections, load_models, poisson_ci, stream, wilson  # noqa: E402

RATE = C.RATE
WORK = Path(os.environ.get("WORK", "/work/workspaces/kivi-wakeword"))
DEFAULT_THRESHOLDS = "0.5,0.7,0.9,0.95,0.97,0.98,0.99"


def default_models():
    return [f"kivi={WORK / 'final/kivi_uncalibrated.tflite'}", "alexa=builtin"]


def score_pcm(feats, models, pcm):
    """{model: probabilities}, step times (s since the first sample): the server's runtime, from sample 0."""
    return stream(feats, models, pcm)


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


def analyze(session, probs, times, coverage, thresholds, pad_before=0.5, pad_after=3.5, refractory=2.0, run_ends=()):
    """Aggregate scores of one source. Times are on the reference clock (s since the pc-mic file
    start); the session's block and cue times are converted with the pc-mic offset."""
    o = C.session_pc_offset(session)
    sess_windows = C.cue_windows(session, pad_before, pad_after)
    windows = [{**w, "t0": w["t0"] - o, "t1": w["t1"] - o} for w in sess_windows]
    segs = [(bi, lab, t0 - o, t1 - o) for bi, lab, t0, t1 in
            C.noncue_segments(session, sess_windows, min_len=0.0, max_len=1e9)]
    models = list(probs)
    blocks = {b["index"]: b for b in session["blocks"]}
    max_in = {}   # (model, window idx) -> max score in the window, or None when the cue is lost
    run_end_in = {}
    for i, w in enumerate(windows):
        ends = [e for e in run_ends if w["t0"] <= e < w["t1"]]
        run_end_in[i] = bool(ends)
        w["t1_eff"] = max(w["t0"], min(ends)) if ends else w["t1"]   # the audio stops at the server's detection
    for m in models:
        for i, w in enumerate(windows):
            t1 = w["t1_eff"]
            if t1 - w["t0"] < (1.0 if run_end_in[i] else 0.1) or covered_fraction(coverage, w["t0"], t1) < 0.9:
                max_in[(m, i)] = None            # lost: kept in the denominators as a miss
                continue
            sel = (times >= w["t0"]) & (times <= t1)
            max_in[(m, i)] = float(probs[m][sel].max()) if sel.any() else 0.0
    dets = {(m, t): np.asarray(detections(probs[m], times, t, refractory)) for m in models for t in thresholds}
    out_blocks = []
    for bi in sorted(blocks):
        b = blocks[bi]
        idx = [i for i, w in enumerate(windows) if w["block"] == bi]
        entry = {"index": bi, "name": b["name"], "kind": b["kind"], "condition": C.condition_string(b),
                 "cues": len(idx), "lost_cues": sum(max_in[(models[0], i)] is None for i in idx) if models else 0,
                 "run_end_cues": sum(run_end_in[i] for i in idx)}
        entry["cue_hits"] = {m: {str(t): rate_cell(sum(1 for i in idx if max_in[(m, i)] is not None and max_in[(m, i)] > t),
                                                    len(idx))
                                 for t in thresholds} for m in models}
        if b["kind"] == "near":
            words = {}
            for i in idx:
                words.setdefault(windows[i]["word"].lower(), []).append(i)
            entry["by_word"] = {wd: {m: {str(t): rate_cell(sum(1 for i in ii if max_in[(m, i)] is not None and max_in[(m, i)] > t),
                                                          len(ii))
                                         for t in thresholds} for m in models} for wd, ii in sorted(words.items())}
        bsegs = [s for s in segs if s[0] == bi]
        hours = sum(covered_seconds(coverage, s[2], s[3]) for s in bsegs) / 3600
        entry["noncue_hours"] = round(hours, 4)
        entry["server_run_ends"] = sum(1 for e in run_ends if any(sg[2] <= e <= sg[3] for sg in bsegs))
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
    res["lost_cues"] = sum(b["lost_cues"] for b in blocks)
    res["run_end_cues"] = sum(b["run_end_cues"] for b in blocks)
    res["server_run_ends"] = sum(b["server_run_ends"] for b in blocks)
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
    ov = rep["overall"]
    L += ["", "### Coverage and server events", "",
          f"- cues in the denominators: {sum(b['cues'] for b in rep['blocks'])}; lost or partly uncovered (counted as misses): {ov['lost_cues']}",
          f"- cues whose window contains the end of a closed wake file (a server detection of the live wake word): {ov['run_end_cues']}",
          f"- file ends outside cue windows (server detections of the live wake word on other sound): {ov['server_run_ends']}"]
    for f in rep.get("alignment_files", []):
        L.append(f"- alignment {f['name']}: {'OK' if f['ok'] else 'NOT TRUSTED'}, "
                 f"{'used' if f['used'] else 'LEFT OUT (use --include-unaligned)'}, median PSR {f['median_conf']}, drift {f['drift_ppm']} ppm")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------------


def load_source(sdir, label, feats, models, include_unaligned=False):
    """Stream every raw file of a dataset source through the server runtime and map the scores to the
    desk-mic clock. Returns (probs per model, times, coverage intervals, run ends, file status)."""
    base = Path(sdir) / label
    als = sorted(base.glob("alignment*.json"), key=lambda p: p.stat().st_mtime)
    if not als:
        raise SystemExit(f"{base}: no alignment.json (run kivi_extract.py first)")
    al = json.loads(als[-1].read_text(encoding="utf-8"))
    parts, cov, ends, status = [], [], [], []
    for f in al["files"]:
        used = bool(f.get("included", True)) and (f["ok"] or include_unaligned)
        status.append({"name": f["name"], "ok": bool(f["ok"]), "used": used, "median_conf": f.get("median_conf"),
                       "drift_ppm": f.get("drift_ppm")})
        if not used or not f.get("coverage_s"):
            continue
        pcm = C.read_pcm16(base / "raw" / f["name"])
        probs, t = score_pcm(feats, models, pcm)
        a_, b_ = f["offset_a_s"], f.get("b", 0.0)
        parts.append((probs, (t - a_) / (1 + b_)))     # file time -> desk-mic time
        cov.append(f["coverage_s"])
        if f.get("run_end_ref_s") is not None:
            ends.append(f["run_end_ref_s"])
    names = list(models)
    if parts:
        times = np.concatenate([t for _, t in parts])
        probs = {n: np.concatenate([p[n] for p, _ in parts]) for n in names}
        order = np.argsort(times, kind="stable")
        times, probs = times[order], {n: v[order] for n, v in probs.items()}
    else:
        times, probs = np.zeros(0), {n: np.zeros(0, np.float32) for n in names}
    return probs, times, cov, ends, status


def score_session(a, feats=None, models=None):
    sdir = Path(a.session).expanduser()
    session = json.loads((sdir / "session.json").read_text(encoding="utf-8"))
    thr = [float(t) for t in a.thresholds.split(",")]
    specs = a.model or default_models()
    if feats is None:
        feats, models = load_models(specs)
    sources = [a.source or session.get("tv_source") or C.source_label(session["device"])]
    if a.also_pc:
        sources += list(C.PC_SOURCES)
    reports = []
    for label in sources:
        probs, times, cov, ends, status = load_source(sdir, label, feats, models, a.include_unaligned)
        rep = analyze(session, probs, times, cov, thr, a.pad_before, a.pad_after, a.refractory, ends)
        rep.update(session=session["session_id"], speaker=session.get("speaker", ""), device=session["device"],
                   split=session.get("split", ""), source=label, alignment_files=status,
                   models={s.split("=", 1)[0]: Path(s.split("=", 1)[1]).name for s in specs},
                   audio_hours=round(sum(c[1] - c[0] for c in cov) / 3600, 4))
        reports.append(rep)
        bad = [f for f in status if not f["used"]]
        if bad:
            print(f"NOTE: {label}: {len(bad)} file(s) with untrusted alignment left out of the scores "
                  "(--include-unaligned to keep them)", file=sys.stderr)
    return reports


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--session", required=True, help="the session directory inside the dataset")
    ap.add_argument("--source", help="satellite source label (default: the session's)")
    ap.add_argument("--also-pc", action="store_true", help="also score pc-mic and webcam-mic, for comparison")
    ap.add_argument("--model", action="append", metavar="NAME=model.tflite|builtin",
                    help="default: kivi (uncalibrated TFLite) and alexa=builtin")
    ap.add_argument("--include-unaligned", action="store_true",
                    help="also score files whose alignment was not trusted (reported as NOT TRUSTED)")
    ap.add_argument("--thresholds", default=DEFAULT_THRESHOLDS)
    ap.add_argument("--pad-before", type=float, default=0.5)
    ap.add_argument("--pad-after", type=float, default=3.5)
    ap.add_argument("--refractory", type=float, default=2.0)
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
