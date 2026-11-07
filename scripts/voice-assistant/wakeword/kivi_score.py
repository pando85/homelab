#!/usr/bin/env python3
"""Score `kivi` and the baseline `alexa` on a real session with the server's own runtime.

    kivi_score.py --session ~/.../datasets/real/<session id>                  # the satellite's audio
    kivi_score.py --session ... --also-pc                                     # plus pc-mic and webcam-mic

Which model was scored is recorded in the report: full path, sha256 and a label (`--label v1|v2`, else
derived from the path). The server-0.5 point of a model (the raw threshold its deployed, calibrated file
moves to 0.5) is NOT assumed: pass `--calibration kivi=0.98` (v1) or `kivi=0.95` (v2), or put
`{"server_0_5_raw": T}` in `<model>.calibration.json`; the point is added to the thresholds and
summarised. `--exclude-cue B4c5` leaves a cue (block 4, cue 5) out of every count. Recall is given in
several forms (see `analyze`): all cues with no-audio cues as misses, and full-audio cues only.
`--negatives DIR` scores the room-audio negatives of `kivi_negatives.py` instead: false accepts per
hour per device with exact Poisson intervals.

Each raw audio file that `kivi_extract.py` stored is streamed from its sample 0 through
`eval_stream.stream`, which is pyopen-wakeword (the library inside wyoming-openwakeword 2.x, TFLite C
API) fed in 1280-sample chunks as 16-bit PCM, with the library's own 8 s startup autofill. No
re-implemented features, no float16 cache. (The 2.1.0 handler does not re-chunk: it resets on AudioStart and passes each
converted AudioChunk straight to process_streaming; only HA's chunk size is unconfirmed.) The score times are mapped to the
desk-mic clock through the stored alignment. Files whose alignment was not trusted are left out
unless `--include-unaligned`. Use the **uncalibrated** kivi model: thresholds are raw.

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
from argparse import Namespace  # noqa: E402

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


def cue_class(frac, full=0.9):
    """'full' (the whole cue window has audio, at least `full` of it), 'partial' (some) or 'none'."""
    return "full" if frac >= full else "partial" if frac > 1e-6 else "none"


def word_span_from_ref(ref_pcm, w_t0, w_t1, ref_offset=0.0, context_s=4.0):
    """The spoken word in the desk-mic audio, by energy only: the loudest non-long burst inside the cue
    window [w_t0, w_t1] (reference clock). Returns (t0, t1, snr_db) or None. No model, no text."""
    lo, hi = max(0.0, w_t0 - context_s), min(len(ref_pcm) / RATE, w_t1 + context_s)
    segs = C.energy_segments(ref_pcm[int(lo * RATE):int(hi * RATE)], lo, margin_db=10.0, min_len_s=0.15, max_len_s=2.5,
                             merge_gap_s=0.3)
    cand = [g for g in segs if not g["long"] and w_t0 - 0.2 <= g["t0"] <= w_t1]
    if not cand:
        return None
    g = max(cand, key=lambda x: x["peak_db"])
    return g["t0"], g["t1"], g["snr_db"]


def analyze(session, probs, times, coverage, thresholds, pad_before=0.5, pad_after=3.5, refractory=2.0, run_ends=(),
            exclude_cues=(), word_spans=None, word_pad=0.25):
    """Aggregate scores of one source. Times are on the reference clock (s since the pc-mic file
    start); the session's block and cue times are converted with the pc-mic offset.

    Every cue is `full` (at least 90 % of its window has audio), `partial` or `none`. Three recall
    forms per block, model and threshold (a cue is a hit when any score inside the covered part of its
    window is above the threshold):
      cue_hits          all cues in the denominator; partial and none count as misses
      cue_hits_full     only cues with full audio (partial and none excluded from numerator and denominator)
      cue_hits_partial  only the partial cues (hits among them)
      cue_hits_word     only cues whose spoken word (the desk-mic burst, `word_spans`, +-`word_pad` s) lies
                        inside the audio (when a cue has no burst, its window class decides)
    `exclude_cues` is a set of (block, cue) that are left out of everything (e.g. a take the speaker
    missed)."""
    to_ref, _ = C.session_to_ref(session)
    exclude_cues = {tuple(x) for x in exclude_cues}
    sess_windows = C.cue_windows(session, pad_before, pad_after)
    windows = [{**w, "t0": float(to_ref(w["t0"])), "t1": float(to_ref(w["t1"]))} for w in sess_windows]
    segs = [(bi, lab, float(to_ref(t0)), float(to_ref(t1))) for bi, lab, t0, t1 in
            C.noncue_segments(session, sess_windows, min_len=0.0, max_len=1e9)]
    models = list(probs)
    blocks = {b["index"]: b for b in session["blocks"]}
    max_in = {}   # (model, window idx) -> max score in the covered part of the window, or None for no audio
    run_end_in = {}
    cls, word_ok = {}, {}
    for i, w in enumerate(windows):
        ends = [e for e in run_ends if w["t0"] <= e < w["t1"]]
        run_end_in[i] = bool(ends)
        w["t1_eff"] = max(w["t0"], min(ends)) if ends else w["t1"]   # the audio stops at the server's detection
        short = w["t1_eff"] - w["t0"] < (1.0 if run_end_in[i] else 0.1)
        frac = 0.0 if short else covered_fraction(coverage, w["t0"], w["t1_eff"])
        cls[i] = cue_class(frac)
        span = (word_spans or {}).get((w["block"], w["cue"]))
        word_ok[i] = (covered_fraction(coverage, span[0] - word_pad, span[1] + word_pad) >= 0.95) if span else cls[i] == "full"
    for m in models:
        for i, w in enumerate(windows):
            if cls[i] == "none":
                max_in[(m, i)] = None
                continue
            sel = (times >= w["t0"]) & (times <= w["t1_eff"])
            max_in[(m, i)] = float(probs[m][sel].max()) if sel.any() else 0.0
    dets = {(m, t): np.asarray(detections(probs[m], times, t, refractory)) for m in models for t in thresholds}

    def cells(idx, m, selector, denom):
        """hits among `idx` cues passing `selector`, over the cues passing `denom`."""
        return {str(t): rate_cell(sum(1 for i in idx if selector(i) and max_in[(m, i)] is not None and max_in[(m, i)] > t),
                                  sum(1 for i in idx if denom(i))) for t in thresholds}

    is_full = lambda i: cls[i] == "full"          # noqa: E731
    everyone = lambda i: True                     # noqa: E731
    out_blocks = []
    for bi in sorted(blocks):
        b = blocks[bi]
        idx = [i for i, w in enumerate(windows) if w["block"] == bi and (bi, w["cue"]) not in exclude_cues]
        entry = {"index": bi, "name": b["name"], "kind": b["kind"], "condition": C.condition_string(b),
                 "cues": len(idx), "full_cues": sum(is_full(i) for i in idx),
                 "partial_cues": sum(cls[i] == "partial" for i in idx), "no_audio_cues": sum(cls[i] == "none" for i in idx),
                 "word_covered_cues": sum(word_ok[i] for i in idx),
                 "excluded_cues": sorted(c for bb, c in exclude_cues if bb == bi),
                 "run_end_cues": sum(run_end_in[i] for i in idx)}
        entry["lost_cues"] = entry["partial_cues"] + entry["no_audio_cues"]
        entry["cue_hits"] = {m: cells(idx, m, is_full, everyone) for m in models}
        entry["cue_hits_full"] = {m: cells(idx, m, is_full, is_full) for m in models}
        entry["cue_hits_partial"] = {m: cells(idx, m, lambda i: cls[i] == "partial", lambda i: cls[i] == "partial") for m in models}
        entry["cue_hits_word"] = {m: cells(idx, m, lambda i: word_ok[i], lambda i: word_ok[i]) for m in models}
        if b["kind"] == "near":
            words = {}
            for i in idx:
                words.setdefault(windows[i]["word"].lower(), []).append(i)
            entry["by_word"] = {wd: {m: cells(ii, m, is_full, everyone) for m in models} for wd, ii in sorted(words.items())}
        bsegs = [sg for sg in segs if sg[0] == bi]
        hours = sum(covered_seconds(coverage, sg[2], sg[3]) for sg in bsegs) / 3600
        entry["noncue_hours"] = round(hours, 4)
        entry["server_run_ends"] = sum(1 for e in run_ends if any(sg[2] <= e <= sg[3] for sg in bsegs))
        entry["false_accepts"] = {}
        for m in models:
            entry["false_accepts"][m] = {}
            for t in thresholds:
                d = dets[(m, t)]
                k = int(sum(1 for x in d for sg in bsegs if sg[2] <= x <= sg[3] and covered_seconds(coverage, x, x + 1e-3) > 0))
                lo, hi = poisson_ci(k, hours) if hours > 0 else (0.0, float("nan"))
                entry["false_accepts"][m][str(t)] = {"k": k, "hours": round(hours, 4),
                                                      "per_hour": round(k / hours, 3) if hours else None,
                                                      "ci95": [round(lo, 3), round(hi, 3)] if hours else None}
        out_blocks.append(entry)
    return {"thresholds": thresholds, "refractory_s": refractory, "pad_s": [pad_before, pad_after],
            "excluded_cues": sorted([list(x) for x in exclude_cues]),
            "word_spans_found": sum(1 for w in windows if (word_spans or {}).get((w["block"], w["cue"])) is not None
                                    and (w["block"], w["cue"]) not in exclude_cues) if word_spans is not None else None,
            "blocks": out_blocks, "overall": overall(out_blocks, models, thresholds)}


FORMS = ("cue_hits", "cue_hits_full", "cue_hits_partial", "cue_hits_word")


def overall(blocks, models, thresholds):
    """Totals over blocks: kivi recall per condition (all forms), near-words in total and per word, false
    accepts per hour over all non-cue time."""
    res = {"kivi_by_condition": {}, "near_total": {}, "near_by_word": {}, "false_accepts": {}}

    def pool(selected, key):
        return {m: {str(t): rate_cell(sum(b[key][m][str(t)]["k"] for b in selected),
                                      sum(b[key][m][str(t)]["n"] for b in selected)) for t in thresholds} for m in models}

    def pool_forms(selected):
        return {key: pool(selected, key) for key in FORMS}

    for cond in sorted({b["condition"] for b in blocks if b["kind"] == "kivi"}):
        res["kivi_by_condition"][cond] = pool_forms([b for b in blocks if b["kind"] == "kivi" and b["condition"] == cond])
    kb = [b for b in blocks if b["kind"] == "kivi"]
    if kb:
        res["kivi_by_condition"]["ALL"] = pool_forms(kb)
    nb = [b for b in blocks if b["kind"] == "near"]
    if nb:
        res["near_total"] = pool_forms(nb)
        for wd in sorted({w for b in nb for w in b.get("by_word", {})}):
            res["near_by_word"][wd] = {m: {str(t): rate_cell(sum(b["by_word"][wd][m][str(t)]["k"] for b in nb if wd in b["by_word"]),
                                                              sum(b["by_word"][wd][m][str(t)]["n"] for b in nb if wd in b["by_word"]))
                                           for t in thresholds} for m in models}
    hours = sum(b["noncue_hours"] for b in blocks)
    for key in ("cues", "full_cues", "partial_cues", "no_audio_cues", "word_covered_cues", "lost_cues", "run_end_cues",
                "server_run_ends"):
        res[key] = sum(b.get(key, 0) for b in blocks)
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


FORM_TITLES = {
    "cue_hits": "counting every cue as a trial: cues without full audio are MISSES (denominator = all cues)",
    "cue_hits_full": "only cues with full audio (partial and no-audio cues EXCLUDED from numerator and denominator)",
    "cue_hits_partial": "partial-audio cues only: hits among them",
    "cue_hits_word": "cues whose spoken word (desk-mic burst) is inside the audio: hits among them",
}


def op_point_summary(rep):
    """Rows for the operating-point table: per model at its server-0.5 raw threshold."""
    ov = rep["overall"]
    rows = []
    for m, info in rep["models"].items():
        t = info.get("server_0_5_raw") if isinstance(info, dict) else None
        if t is None:
            continue
        k = str(t)
        if not rep["blocks"] or k not in rep["blocks"][0]["false_accepts"][m]:
            continue
        kb = ov["kivi_by_condition"].get("ALL")
        rows.append({"model": m, "label": info.get("label"), "raw_threshold": t,
                     "recall_all_cues_misses": _cell(kb["cue_hits"][m][k]) if kb else "n/a",
                     "recall_full_audio_only": _cell(kb["cue_hits_full"][m][k]) if kb else "n/a",
                     "near_words": _cell(ov["near_total"]["cue_hits"][m][k]) if ov["near_total"] else "n/a",
                     "false_accepts": ov["false_accepts"][m][k]})
    return rows


def to_markdown(rep):
    thr = rep["thresholds"]
    head = "| " + " | ".join(["", "model"] + [f"> {t}" for t in thr]) + " |\n|" + " --- |" * (len(thr) + 2) + "\n"
    L = [f"## {rep['source']} - session {rep['session']} (speaker {rep.get('speaker') or '?'}, "
         f"device {rep.get('device')}, split {rep.get('split')})", ""]
    for m, p in rep["models"].items():
        if isinstance(p, dict):
            op = p.get("server_0_5_raw")
            L.append(f"- model `{m}` (label `{p.get('label')}`): `{p.get('path')}`, sha256 `{p.get('sha256')}`, "
                     + (f"server 0.5 = raw {op}" if op is not None else "no calibration given"))
        else:
            L.append(f"- model `{m}`: {p}")
    ex = rep.get("excluded_cues") or []
    sf = rep.get("scored_from")
    if sf:
        L.append(f"- scored from extraction revision `{sf.get('revision') or '(default)'}` (extract {sf.get('extract_id')}, "
                 f"{sf.get('alignment_file')}, session-to-desk-mic time map: {sf.get('ref_map')}"
                 + (f"; superseded records skipped: {', '.join(sf['superseded_alignment_files_skipped'])}"
                    if sf.get("superseded_alignment_files_skipped") else "") + ")")
    L += ["", f"Raw thresholds (the uncalibrated head; each model's server-0.5 point is listed above). Cue window "
          f"-{rep['pad_s'][0]} s .. +{rep['pad_s'][1]} s. Detections use a {rep['refractory_s']} s refractory time."
          + (f" Cues left out of every count: {', '.join(f'B{b}c{c}' for b, c in ex)}." if ex else ""), ""]
    ov = rep["overall"]
    models = list(rep["models"])
    ops = op_point_summary(rep)
    if ops:
        L += ["### At each model's server-0.5 operating point", "",
              "| model | raw threshold | recall, all cues (no-audio = miss) | recall, full-audio cues only | near words | false accepts |",
              "| --- | --- | --- | --- | --- | --- |"]
        for r in ops:
            fa = r["false_accepts"]
            L.append(f"| {r['model']} | {r['raw_threshold']} | {r['recall_all_cues_misses']} | {r['recall_full_audio_only']} | "
                     f"{r['near_words']} | {fa['k']} in {fa['hours']} h |")
        L.append("")

    def block_table(title, rows):
        L.append(f"### {title}\n")
        L.append(head.rstrip("\n"))
        for name, per_model in rows:
            for m in models:
                L.append("| " + " | ".join([name, m] + [_cell(per_model[m][str(t)]) for t in thr]) + " |")
        L.append("")

    kbk = [b for b in rep["blocks"] if b["kind"] == "kivi"]
    if kbk:
        for form in FORMS:
            rows = [(b["name"] + f" ({b['condition']})", b[form]) for b in kbk]
            rows += [(f"{c} (pooled)", v[form]) for c, v in ov["kivi_by_condition"].items() if c != "ALL"]
            rows += [("ALL", ov["kivi_by_condition"]["ALL"][form])]
            block_table(f"Recall on \"kivi\" cues per block: {FORM_TITLES[form]}", rows)
    if ov["near_total"]:
        block_table("Near-word false accepts (cues with full audio that scored above the threshold; no-audio cues count as no hit)",
                    [("all near words", ov["near_total"]["cue_hits"])] + [(w, v) for w, v in ov["near_by_word"].items()])
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
    L += ["", "### Coverage and server events", "",
          f"- cues in the denominators: {ov['cues']}; audio for the whole window (full): {ov['full_cues']}; partial: {ov['partial_cues']}; "
          f"no audio: {ov['no_audio_cues']}; spoken word inside the audio (desk-mic burst, +-0.25 s): {ov['word_covered_cues']}",
          f"- cues whose window contains the end of a closed wake file (a server detection of the live wake word): {ov['run_end_cues']}",
          f"- file ends outside cue windows (server detections of the live wake word on other sound): {ov['server_run_ends']}"]
    for f in rep.get("alignment_files", []):
        L.append(f"- alignment {f['name']} ({f.get('stage') or 'ref'}): {'OK' if f['ok'] else 'NOT TRUSTED'}, "
                 f"{'used' if f['used'] else 'LEFT OUT (use --include-unaligned)'}, median PSR {f['median_conf']}, drift {f['drift_ppm']} ppm")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------------------------
# models: identity, label, calibration
# ---------------------------------------------------------------------------------------------


def file_sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def derive_label(path):
    """A label from the path: relative to the work directory when inside it, with / as -, no extension
    (`v2/final/kivi_uncalibrated.tflite` -> `v2-final-kivi_uncalibrated`)."""
    p = Path(path).resolve()
    try:
        rel = p.relative_to(WORK.resolve())
    except ValueError:
        rel = Path(*p.parts[-3:])
    return "-".join(rel.with_suffix("").parts)


def model_infos(specs, calibration=None, label=None):
    """{name: {path, sha256, label, server_0_5_raw}}. The server-0.5 point of a model is the raw
    threshold that its deployed (calibrated) file moves to 0.5: it comes from `calibration`
    ({name: T}), else from `<model file>.calibration.json` (`{"server_0_5_raw": T}`), else it is
    unknown (None) and the report says so. The built-in `alexa` is not calibrated (0.5). `label` names the
    model file when there is exactly one; otherwise (and without `--label`) the label comes from the path."""
    calibration = dict(calibration or {})
    n_files = sum(1 for sp in specs if sp.split("=", 1)[1] != "builtin")
    out = {}
    for spec in specs:
        name, path = spec.split("=", 1)
        if path == "builtin":
            out[name] = {"path": "builtin", "sha256": None, "label": name, "server_0_5_raw": 0.5}
            continue
        side = Path(str(path) + ".calibration.json")
        t = calibration.get(name)
        src = "parameter"
        if t is None and side.exists():
            t, src = float(json.loads(side.read_text(encoding="utf-8"))["server_0_5_raw"]), "sidecar"
        if t is None:
            src = None
        out[name] = {"path": str(Path(path).resolve()), "sha256": file_sha256(path),
                     "label": label if (label and n_files == 1) else derive_label(path),
                     "server_0_5_raw": float(t) if t is not None else None, "calibration_source": src}
    return out


def parse_calibration(items):
    out = {}
    for it in items or []:
        name, _, t = it.partition("=")
        if not name or not t:
            raise SystemExit(f"--calibration needs NAME=RAW_THRESHOLD, got {it!r}")
        out[name] = float(t)
    return out


def parse_cue_ids(items):
    """['B4c5'] -> {(4, 5)}."""
    import re
    out = set()
    for it in items or []:
        m = re.fullmatch(r"[Bb](\d+)[Cc](\d+)", it.strip())
        if not m:
            raise SystemExit(f"--exclude-cue needs B<block>c<cue>, e.g. B4c5, got {it!r}")
        out.add((int(m.group(1)), int(m.group(2))))
    return out


def thresholds_with_ops(thr_text, infos, extra=(0.9,)):
    thr = {float(t) for t in thr_text.split(",")} | set(extra)
    thr |= {i["server_0_5_raw"] for i in infos.values() if i.get("server_0_5_raw") is not None}
    return sorted(thr)


# ---------------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------------


def load_source(sdir, label, feats, models, include_unaligned=False):
    """Stream every raw file of a dataset source through the server runtime and map the scores to the
    desk-mic clock. Returns (probs per model, times, coverage intervals, run ends, file status)."""
    base = Path(sdir) / label
    al, meta = pick_alignment(sdir, label)
    parts, cov, ends, status = [], [], [], []
    for f in al["files"]:
        used = bool(f.get("included", True)) and (f["ok"] or include_unaligned)
        status.append({"name": f["name"], "stage": f.get("stage"), "ok": bool(f["ok"]), "used": used,
                       "median_conf": f.get("median_conf"), "drift_ppm": f.get("drift_ppm")})
        if not used or not f.get("coverage_s"):
            continue
        pcm = C.read_pcm16(base / "raw" / f["name"])
        probs, t = score_pcm(feats, models, pcm)
        parts.append((probs, C.curve_ref_time(f.get("curve"), f["offset_a_s"], f.get("b", 0.0), t)))   # file time -> desk-mic time
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
    return probs, times, cov, ends, status, meta


def pick_alignment(sdir, label):
    """The newest alignment record of a source whose extraction revision is not superseded
    (`superseded.jsonl`), and where it came from: file, extract id, revision, time mapping."""
    base = Path(sdir) / label
    als = sorted(base.glob("alignment*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not als:
        raise SystemExit(f"{base}: no alignment.json (run kivi_extract.py first)")
    sid = Path(sdir).name
    sup = C.read_jsonl(Path(sdir).parent / "superseded.jsonl")
    skipped = []
    for p in als:
        al = json.loads(p.read_text(encoding="utf-8"))
        rep_file = Path(sdir) / f"extract-{al.get('extract_id')}.json"
        rev = ""
        if rep_file.exists():
            rev = json.loads(rep_file.read_text(encoding="utf-8")).get("revision") or ""
        if any(m["session"] == sid and (m.get("revision") or "") == rev and m.get("source") in (None, label) for m in sup):
            skipped.append(p.name)
            continue
        return al, {"alignment_file": p.name, "extract_id": al.get("extract_id"), "revision": rev,
                    "ref_map": al.get("ref_map", "offset"), "superseded_alignment_files_skipped": skipped}
    raise SystemExit(f"{base}: every alignment record belongs to a superseded revision")


def ref_word_spans(sdir, session, a):
    """{(block, cue): (t0, t1)} on the reference clock from the desk-mic file in the dataset (energy only)."""
    pcw = Path(sdir) / "pc-mic" / "raw" / "pc-mic.wav"
    if not pcw.exists():
        return None
    pc = C.read_pcm16(pcw)
    to_ref, _ = C.session_to_ref(session)
    spans = {}
    for w in C.cue_windows(session, a.pad_before, a.pad_after):
        g = word_span_from_ref(pc, float(to_ref(w["t0"])), float(to_ref(w["t1"])))
        spans[(w["block"], w["cue"])] = (g[0], g[1]) if g else None
    return spans


def score_session(a, feats=None, models=None):
    sdir = Path(a.session).expanduser()
    session = json.loads((sdir / "session.json").read_text(encoding="utf-8"))
    specs = a.model or default_models()
    infos = model_infos(specs, parse_calibration(getattr(a, "calibration", None)), getattr(a, "label", None))
    thr = thresholds_with_ops(a.thresholds, infos)
    excl = parse_cue_ids(getattr(a, "exclude_cue", None))
    if feats is None:
        feats, models = load_models(specs)
    sources = [a.source or session.get("tv_source") or C.source_label(session["device"])]
    if a.also_pc:
        sources += list(C.PC_SOURCES)
    spans = ref_word_spans(sdir, session, a)
    reports = []
    for label in sources:
        probs, times, cov, ends, status, meta = load_source(sdir, label, feats, models, a.include_unaligned)
        rep = analyze(session, probs, times, cov, thr, a.pad_before, a.pad_after, a.refractory, ends,
                      exclude_cues=excl, word_spans=spans)
        rep.update(session=session["session_id"], speaker=session.get("speaker", ""), device=session["device"],
                   split=C.session_split(sdir.parent, session), source=label, alignment_files=status, models=infos,
                   scored_from=meta,
                   run_label=getattr(a, "label", None) or "-".join(i["label"] for i in infos.values()),
                   audio_hours=round(sum(c[1] - c[0] for c in cov) / 3600, 4))
        reports.append(rep)
        bad = [f for f in status if not f["used"]]
        if bad:
            print(f"NOTE: {label}: {len(bad)} file(s) with untrusted alignment left out of the scores "
                  "(--include-unaligned to keep them)", file=sys.stderr)
    return reports


# ---------------------------------------------------------------------------------------------
# room-audio negatives (kivi_negatives.py output): false accepts per hour per device
# ---------------------------------------------------------------------------------------------


def _neg_worker(args):
    paths, specs = args
    feats, models = load_models(specs)
    out = []
    for p in paths:
        pcm = C.read_pcm16(p)
        probs, times = stream(feats, models, pcm)
        out.append((str(p), len(pcm) / RATE, {m: v for m, v in probs.items()}, times))
    return out


def count_detections(probs, times, thresholds, refractory):
    return {str(t): len(detections(probs, times, t, refractory)) for t in thresholds}


def score_negatives(a, specs=None):
    """Stream every negative piece (separately, from sample 0, like a new run) through the server runtime and
    count detections per device and threshold with the refractory time; hours = summed piece durations.
    Returns aggregates only (no clip names)."""
    import multiprocessing as mp
    root = Path(a.negatives).expanduser()
    rows = [r for r in C.read_manifest(root) if not r.get("superseded")]    # marks in <negatives>/superseded.jsonl, if any
    specs = specs or a.model or default_models()
    infos = model_infos(specs, parse_calibration(getattr(a, "calibration", None)), getattr(a, "label", None))
    thr = thresholds_with_ops(a.thresholds, infos)
    by_dev = {}
    for r in rows:
        by_dev.setdefault(r["device"], []).append(root.parent / r["clip"])
    jobs = int(getattr(a, "jobs", 1))
    tasks = []
    for dev, paths in by_dev.items():
        for i in range(0, len(paths), 8):
            tasks.append((dev, paths[i:i + 8]))
    res = {dev: {"pieces": len(p), "hours": 0.0, "detections": {m: {str(t): 0 for t in thr} for m in infos}}
           for dev, p in by_dev.items()}
    def consume(n, dev, out):
        for _, dur, probs, times in out:
            res[dev]["hours"] += dur / 3600
            for m in infos:
                for t, k in count_detections(probs[m], times, thr, a.refractory).items():
                    res[dev]["detections"][m][t] += k
        if n % 10 == 9 or n + 1 == len(tasks):
            print(f"{n + 1}/{len(tasks)} batches", file=sys.stderr, flush=True)

    payload = [(t[1], specs) for t in tasks]
    if jobs <= 1:
        for n, (task, pl) in enumerate(zip(tasks, payload)):
            consume(n, task[0], _neg_worker(pl))
    else:
        with mp.get_context("spawn").Pool(jobs) as pool:
            for n, (task, out) in enumerate(zip(tasks, pool.imap(_neg_worker, payload))):
                consume(n, task[0], out)
    total_h = sum(d["hours"] for d in res.values())
    rep = {"kind": "negatives", "models": infos, "thresholds": thr, "refractory_s": a.refractory, "devices": {},
           "pooled": {"hours": round(total_h, 3), "pieces": sum(d["pieces"] for d in res.values()), "false_accepts": {}}}
    for dev, d in res.items():
        rep["devices"][dev] = {"pieces": d["pieces"], "hours": round(d["hours"], 3), "false_accepts": {}}
    for m in infos:
        for scope, hrs, ks in [(dev, d["hours"], d["detections"][m]) for dev, d in res.items()] + \
                              [("pooled", total_h, {str(t): sum(d["detections"][m][str(t)] for d in res.values()) for t in thr})]:
            cells = {}
            for t in thr:
                k = ks[str(t)]
                lo, hi = poisson_ci(k, hrs) if hrs > 0 else (0.0, float("nan"))
                cells[str(t)] = {"k": int(k), "hours": round(hrs, 3), "per_hour": round(k / hrs, 3) if hrs else None,
                                 "ci95": [round(lo, 3), round(hi, 3)] if hrs else None}
            (rep["pooled"] if scope == "pooled" else rep["devices"][scope])["false_accepts"][m] = cells
    return rep


def negatives_markdown(rep):
    thr = rep["thresholds"]
    L = ["## Room-audio negatives (household TV-room sound, no spoken cues)", ""]
    for m, p in rep["models"].items():
        L.append(f"- model `{m}` (label `{p.get('label')}`): `{p.get('path')}`, sha256 `{p.get('sha256')}`, "
                 + (f"server 0.5 = raw {p['server_0_5_raw']}" if p.get("server_0_5_raw") is not None else "no calibration given"))
    L += ["", f"Pieces are scored separately from sample 0; {rep['refractory_s']} s refractory. Intervals: exact Poisson 95 %.", "",
          "### At each model's server-0.5 operating point", "", "| scope | hours | model | raw threshold | false accepts | per hour [95 % CI] |",
          "| --- | --- | --- | --- | --- | --- |"]
    scopes = list(rep["devices"].items()) + [("pooled", rep["pooled"])]
    for name, d in scopes:
        for m, p in rep["models"].items():
            t = p.get("server_0_5_raw")
            if t is None:
                continue
            c = d["false_accepts"][m][str(t)]
            L.append(f"| {name} | {d['hours']} | {m} | {t} | {c['k']} | {c['per_hour']} [{c['ci95'][0]}, {c['ci95'][1]}] |")
    L += ["", "### All thresholds (false accepts, per hour)", "", "| scope | model | " + " | ".join(f"> {t}" for t in thr) + " |",
          "|" + " --- |" * (len(thr) + 2)]
    for name, d in scopes:
        for m in rep["models"]:
            L.append("| " + " | ".join([name, m] + [f"{d['false_accepts'][m][str(t)]['k']} ({d['false_accepts'][m][str(t)]['per_hour']}/h)"
                                                   for t in thr]) + " |")
    return "\n".join(L) + "\n"


def score_button_takes(a, feats=None, models=None):
    """Recall on the takes of a `kivi_extract.py --button-takes` session: the raw wake file(s) of the extraction
    are streamed once through the server runtime and each take's score is the maximum of the model output inside
    the take's window (the extractor's own `take_window`, from the extraction record). A take with no speech
    counts as a miss; takes whose window is not covered by the audio are `no_audio` misses. Aggregates only."""
    import kivi_extract as X
    sdir = Path(a.button_takes).expanduser()
    rev = a.revision if a.revision is not None else X.SAY_THEN_PRESS_REVISION
    recs = sorted(sdir.glob("extract-*.json"))
    recs = [r for r in ((p, json.loads(p.read_text(encoding="utf-8"))) for p in recs) if (r[1].get("revision") or "") == rev
            and r[1].get("takes_log")]
    if not recs:
        raise SystemExit(f"no extraction record with revision {rev!r} (and a takes_log) in {sdir}")
    path, ex = recs[-1]
    specs = a.model or default_models()
    infos = model_infos(specs, parse_calibration(getattr(a, "calibration", None)), getattr(a, "label", None))
    thr = thresholds_with_ops(a.thresholds, infos)
    if feats is None:
        feats, models = load_models(specs)
    tv = ex.get("tv_source") or next(p.name for p in sdir.iterdir() if p.is_dir() and p.name.startswith("tv-"))
    parts = []
    for rf in ex["raw_files"]:
        t0 = X.parse_when(rf["t0_utc"])
        probs, t = score_pcm(feats, models, C.read_pcm16(sdir / tv / "raw" / rf["name"]))
        parts.append((t0, probs, t))
    w = Namespace(before_s=ex["window_s"]["before"], after_s=ex["window_s"]["after"],
                  min_gap_after_prev_s=ex["window_s"]["min_gap_after_prev"], window_s=ex["window_s"]["window"],
                  tolerance_s=ex["window_s"]["tolerance"])
    excl = {int(x) for x in (a.exclude_take or [])}
    best = {m: [] for m in models}
    for tk in ex["takes_log"]:
        press = X.parse_when(tk["press_utc"])
        prev = X.parse_when(tk["prev_last_press_utc"]) if tk.get("prev_last_press_utc") else None
        lo, hi = X.take_window(ex["button_order"], press, prev, w)
        for m in models:
            v = None
            for t0, probs, t in parts:
                sel = (t0 + t >= lo) & (t0 + t <= hi)
                cover = max(0.0, min(hi, t0 + t[-1]) - max(lo, t0 + t[0] - 0.08)) / max(1e-9, hi - lo)
                if sel.any() and cover >= 0.9:               # the window must be (nearly) inside the audio
                    mx = float(probs[m][sel].max())
                    v = mx if v is None else max(v, mx)
            best[m].append(v)
    n_takes = len(ex["takes_log"])
    take_ids = [tk["take"] for tk in ex["takes_log"]]
    rep = {"kind": "button-takes", "session": sdir.name, "extract_record": path.name, "revision": rev,
           "button_order": ex["button_order"], "phrase": ex.get("phrase", "kivi"), "takes": n_takes, "presses": ex["presses"],
           "double_presses": ex["double_presses"], "excluded_takes": sorted(excl), "models": infos, "thresholds": thr,
           "no_audio_takes": sorted({take_ids[i] for m in models for i, v in enumerate(best[m]) if v is None}),
           "median_of_max": {m: round(float(np.median([v or 0.0 for v in best[m]])), 4) for m in models}, "recall": {}}
    for m in models:
        rep["recall"][m] = {}
        for t in thr:
            hits = [v is not None and v > t for v in best[m]]
            rep["recall"][m][str(t)] = {"all_takes": rate_cell(sum(hits), n_takes),
                                        "without_excluded": rate_cell(sum(h for h, i in zip(hits, take_ids) if i not in excl),
                                                                      sum(1 for i in take_ids if i not in excl))}
    return rep


def button_takes_markdown(rep):
    L = [f"## button takes - session {rep['session']} (revision {rep['revision']}, order {rep['button_order']}, "
         f"phrase {rep['phrase']})", "",
         f"{rep['takes']} takes from {rep['presses']} presses ({rep['double_presses']} double); takes left out in the second "
         f"figure: {rep['excluded_takes'] or 'none'}.", ""]
    for m, p in rep["models"].items():
        op = p.get("server_0_5_raw")
        L.append(f"- model `{m}` (label `{p.get('label')}`): `{p.get('path')}`, sha256 `{p.get('sha256')}`, "
                 + (f"server 0.5 = raw {op}" if op is not None else "no calibration given"))
    L += ["", "| model | raw threshold | all takes | without excluded | median of max score |", "| --- | --- | --- | --- | --- |"]
    for m, p in rep["models"].items():
        for t in rep["thresholds"]:
            if t == p.get("server_0_5_raw") or t in (0.9,):
                c, d = rep["recall"][m][str(t)]["all_takes"], rep["recall"][m][str(t)]["without_excluded"]
                L.append(f"| {m} | {t}{' (server 0.5)' if t == p.get('server_0_5_raw') else ''} | {c['k']}/{c['n']} "
                         f"[{c['ci95'][0]}, {c['ci95'][1]}] | {d['k']}/{d['n']} | {rep['median_of_max'][m]} |")
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--button-takes", help="score a `kivi_extract.py --button-takes` session directory instead of a "
                                           "kivi_session.py session (recall per take, window of the extraction)")
    ap.add_argument("--revision", help="(--button-takes) extraction revision (default: the say-then-press one)")
    ap.add_argument("--exclude-take", action="append", type=int, help="(--button-takes) take number for the 'without excluded' figure")
    ap.add_argument("--session", help="the session directory inside the dataset")
    ap.add_argument("--negatives", help="the dataset's negatives directory (kivi_negatives.py output): score it instead of a session")
    ap.add_argument("--source", help="satellite source label (default: the session's)")
    ap.add_argument("--also-pc", action="store_true", help="also score pc-mic and webcam-mic, for comparison")
    ap.add_argument("--model", action="append", metavar="NAME=model.tflite|builtin",
                    help="default: kivi (uncalibrated TFLite) and alexa=builtin")
    ap.add_argument("--label", help="a name for this run's model (e.g. v1, v2); default: derived from the model path")
    ap.add_argument("--calibration", action="append", metavar="NAME=RAW",
                    help="the raw threshold that the model's deployed (calibrated) file moves to server 0.5 "
                         "(v1 0.98, v2 0.95); else a sidecar <model>.calibration.json {\"server_0_5_raw\": T}")
    ap.add_argument("--exclude-cue", action="append", metavar="B<block>c<cue>",
                    help="leave a cue out of every count (e.g. B4c5, a take the speaker missed)")
    ap.add_argument("--include-unaligned", action="store_true",
                    help="also score files whose alignment was not trusted (reported as NOT TRUSTED)")
    ap.add_argument("--thresholds", default=DEFAULT_THRESHOLDS)
    ap.add_argument("--pad-before", type=float, default=0.5)
    ap.add_argument("--pad-after", type=float, default=3.5)
    ap.add_argument("--refractory", type=float, default=2.0)
    ap.add_argument("--jobs", type=int, default=3, help="(--negatives) worker processes")
    ap.add_argument("--json", help="write the JSON here (default: <session>/scores/)")
    ap.add_argument("--md", help="write the markdown here (default: <session>/scores/)")
    ap.add_argument("--no-save", action="store_true")
    a = ap.parse_args(argv)
    if sum(bool(x) for x in (a.session, a.negatives, a.button_takes)) != 1:
        raise SystemExit("give exactly one of --session, --negatives and --button-takes")
    if a.button_takes:
        reports = score_button_takes(a)
        md = button_takes_markdown(reports)
        base = Path(a.button_takes).expanduser()
    elif a.negatives:
        rep = score_negatives(a)
        md = negatives_markdown(rep)
        reports = rep
        base = Path(a.negatives).expanduser()
    else:
        reports = score_session(a)
        md = "\n".join(to_markdown(r) for r in reports)
        base = Path(a.session).expanduser()
    print(md)
    if not a.no_save:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        tag = (a.label or "run")
        d = Path(a.json).parent if a.json else C.private_mkdir(base / "scores", base)
        jp = Path(a.json) if a.json else d / f"score-{tag}-{stamp}.json"
        mp_ = Path(a.md) if a.md else d / f"score-{tag}-{stamp}.md"
        C.write_json_new(jp, reports)
        with C.create_exclusive(mp_) as f:
            f.write(md.encode("utf-8"))
        print(f"saved {jp} and {mp_}")


if __name__ == "__main__":
    main()
