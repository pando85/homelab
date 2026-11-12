#!/usr/bin/env python3
"""Benchmark of every "oye Kivi" model version on the newest labels (kivi-models docs/plan/2026-11-false-wake-plan.md
section 12). Re-runnable: labels are read fresh, embeddings and detections are cached. Report only: nothing here selects a
model, an arm or a threshold. Counts and scores only; no audio is played, printed or transcribed.

    kivi_bench.py run [--jobs 7] [--model NAME=FILE_STEM:RAW ...]   # embed + score + report
    kivi_bench.py embed | score | report                             # the steps one by one

Models: v5, v6, v6.2 (release files), the v6.3 arms p3a / p3b / p3c (`v6p3/runs`, at their recall-matched raw value of
plan 9.5) and the v6.3r arms (`v6p3r/runs`, from `v6p3r/selection.json` when it exists: the selected threshold of an
eligible arm, else its recall-matched value), each at its own raw threshold, through the server's runtime
(pyopen-wakeword TFLite) on the server grid.

Material (labels read at run time; `kivi_clean.window_rows` = every window table; live clips `live/live_wakes.csv`):
  (a) calls: live clips labelled true (owner, owner-button, trusted auto, older no-source), detected when the model
      crosses within [tail end - 3 s, tail end + 1 s] of tail + request audio streamed on the server grid
      (`kivi_fair_fa.score_clip`); owner-labelled attempt windows (every table, unique per TV within 3 s), detected
      when the model has a detection (above raw, 2 s refractory) within 3 s on the room audio at the server grid.
      Speaker: the owner's speaker label only (anything else is `unknown`).
  (b) false material: live clips labelled false (same sources), fired when the model crosses anywhere in tail +
      request audio; windows labelled false (owner other-speech / tv-media / noise, trusted auto other-speech / noise;
      unique per TV within 3 s; an attempt within 3 s wins), fired when a detection is within 3 s.
  (c) room moments of `labelq/moments.json` (cached-embedding scan of v5, v6, v6.2, p3a-c at >= 0.3), split by the
      labels within 3 s: false, attempt (owner), unlabelled (waiting, unclear or none); fired as in (b).
  (d) synthetic: C5 held-out-voice positive sets (clean, TV 10 dB, quiet; per clip maximum of the ONNX head, as
      v6_seq_eval.py) and the decoy evaluation clips (787, held-out voices; a clip fires when its maximum is above raw).
Room items need a piece with a known server grid (`kivi_replay_mine.read_pieces`, src_sample0 exact or bracket middle);
the others are counted as "no server-grid audio". Each item is replayed from 13 s before to 3.2 s after it (4 s warm-up,
which gives bit-identical scores to the 60 s of v6_replay.py), detections by the server rule from the range start.
Day split: odd Madrid days from 2026-11-09 on are TEST (honest for every model); even days and the grandfathered
2026-11-05 to -08 are TRAIN, shown per day group with the in-sample cells marked (T = trained on, S = threshold or
arm chosen on it). 95 % intervals: Clopper-Pearson.

Outputs ($KIVI_HOME/audit/bench/, private): BENCH.md, bench.json, emb/ (server-grid embeddings per range, no audio),
dets/ (detection times per model and range), live/ (per model and clip).
"""
import argparse
import csv
import hashlib
import json
import multiprocessing as mp
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import kivi_real_common as C  # noqa: E402

KH = C.kivi_home()
ROOT = KH / "datasets" / "real"
OUT = KH / "audit" / "bench"
RATE, CHUNK = 16000, 1280
MATCH_S = 3.0
PRE_S, POST_S, WARM_S, MERGE_GAP_S = 13.0, 3.2, 4.0, 8.0
REFRACTORY_S = 2.0
ROOM_SETS = ("negatives-daily", "negatives-v5era", "negatives-1111eve", "negatives")
TRUSTED = ("owner", "owner-button", "auto-v1", "")
OWNER = ("owner", "owner-button")
FALSE_WIN = ("other-speech", "tv-media", "noise")
AUTO_FALSE_WIN = ("other-speech", "noise")
C5_SETS = (("clean", "vnext/feat/eval_pos_clean.npz"), ("tv10", "vnext/feat/eval_pos_snr10.npz"),
           ("quiet", "vnext/feat/eval_pos_quiet.npz"))
DECOY_EVAL = ("v6p3r/decoys/feat/eval_decoy_clean.npz", "v6p3r/decoys/eval.manifest.csv")
FIRST_TEST = "2026-11-09"
# name: (file stem under $KIVI_HOME without .tflite / .onnx, raw threshold, in-sample days {day group: mark})
BASE_MODELS = {
    "v5": ("releases/v5-oye-kivi-2026-11-08/model/oye_kivi_next_uncalibrated", 0.9754, {"<=11-08": "T"}),
    "v6": ("releases/v6-oye-kivi-2026-11-11/model/oye_kivi_ctx_uncalibrated", 0.9595, {"<=11-08": "T", "11-10": "T+S"}),
    "v6.2": ("releases/v6-p2-oye-kivi-2026-11-12/model/oye_kivi_ctx2_uncalibrated", 0.9825,
             {"<=11-08": "T", "11-10": "T+S"}),
}
P3_SEEN = {"<=11-08": "T", "11-10": "T+S"}          # v6p3: room 11-05/-06/-08, live and windows 11-08/-10; selection 11-10
P3R_SEEN = {"<=11-08": "T", "11-10": "S", "11-12": "T"}   # v6p3r: 11-05 to -08 and 11-12; selection 11-10


# ---------------------------------------------------------------------------------------------
# small helpers (pure)
# ---------------------------------------------------------------------------------------------


def madrid(t):
    from zoneinfo import ZoneInfo
    lt = datetime.fromtimestamp(t, ZoneInfo("Europe/Madrid"))
    return lt.date().isoformat(), lt.hour >= 18


def day_group(day):
    """TEST for odd Madrid days from 2026-11-09 on; TRAIN groups: <=11-08 (grandfathered and 11-08), then each even day."""
    if day >= FIRST_TEST and int(day[-2:]) % 2:
        return "TEST"
    return "<=11-08" if day <= "2026-11-08" else day[5:]


def clopper(k, n):
    if n == 0:
        return (0.0, 0.0)
    from scipy.stats import beta
    lo = 0.0 if k == 0 else float(beta.ppf(0.025, k, n - k + 1))
    hi = 1.0 if k == n else float(beta.ppf(0.975, k + 1, n - k))
    return (round(lo, 3), round(hi, 3))


def detections(times, probs, thr, refractory=REFRACTORY_S):
    out, last = [], -1e9
    for i in np.nonzero(probs > thr)[0]:
        if times[i] - last >= refractory:
            out.append(float(times[i]))
            last = times[i]
    return out


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()[:12]


def stamp_epoch(s):
    return datetime.strptime(s, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------------------------


def models(extra=()):
    """{name: {tflite, onnx, raw, seen, key}} of every benchmarked model that exists on disk."""
    out = {}
    for n, (stem, raw, seen) in BASE_MODELS.items():
        out[n] = {"tflite": KH / f"{stem}.tflite", "onnx": KH / f"{stem}.onnx", "raw": raw, "seen": seen}
    sel = KH / "v6p3" / "selection.json"
    if sel.exists():
        for n, info in json.loads(sel.read_text())["arms"].items():
            d = KH / "v6p3" / "runs" / n
            out[n] = {"tflite": d / "oye_kivi_uncalibrated.tflite", "onnx": d / "kivi.onnx",
                      "raw": float(info["t_recall_matched"]), "seen": P3_SEEN}
    sel = KH / "v6p3r" / "selection.json"
    if sel.exists():
        s = json.loads(sel.read_text())
        for n, info in s["arms"].items():
            d = KH / "v6p3r" / "runs" / n
            raw = float(info["threshold"]) if info.get("eligible") else float(info["tries"][0])
            tag = "chosen" if (s.get("chosen") or {}).get("arm") == n else ("eligible" if info.get("eligible") else "out")
            out[f"v6.3r-{n}"] = {"tflite": d / "oye_kivi_uncalibrated.tflite", "onnx": d / "kivi.onnx", "raw": raw,
                                 "seen": P3R_SEEN, "note": tag}
    for spec in extra:
        n, rest = spec.split("=", 1)
        stem, raw = rest.rsplit(":", 1)
        out[n] = {"tflite": KH / f"{stem}.tflite", "onnx": KH / f"{stem}.onnx", "raw": float(raw), "seen": {}}
    out = {n: m for n, m in out.items() if m["tflite"].exists()}
    for m in out.values():
        m["key"] = f"{file_sha(m['tflite'])}@{m['raw']:.4f}"
    return out


# ---------------------------------------------------------------------------------------------
# items (labels read fresh)
# ---------------------------------------------------------------------------------------------


def window_class(w, pending):
    lab, src = w["label"], w["source"]
    if not lab:
        return "pending"
    if lab == "attempt":
        return "attempt" if src in OWNER else "pending"
    if lab == "unclear":
        return "unclear"
    if lab in FALSE_WIN and (src in OWNER or (src.startswith("auto") and lab in AUTO_FALSE_WIN)):
        return "false"
    return "other"


PRIO = ("attempt", "false", "unclear", "pending", "other")


def window_clusters():
    """Window rows of every table chained per TV within 3 s: [{device, t, cls, speaker, source, rows}]."""
    import kivi_clean as KC
    rows = KC.window_rows()
    pend = KC.plan_pending_refs()
    for w in rows:
        w["cls"] = window_class(w, pend)
    out = []
    for dev in sorted({w["device"] for w in rows}):
        ws = sorted((w for w in rows if w["device"] == dev), key=lambda w: w["t"])
        cur = []
        for w in ws + [None]:
            if cur and (w is None or w["t"] - cur[-1]["t"] > MATCH_S):
                cls = min((x["cls"] for x in cur), key=PRIO.index)
                head = next(x for x in cur if x["cls"] == cls)
                out.append({"device": dev, "t": head["t"], "cls": cls, "rows": len(cur),
                            "speaker": head.get("speaker") if head["source"] in OWNER and head.get("speaker") else "unknown",
                            "source": "owner" if any(x["source"] in OWNER and x["cls"] == cls for x in cur) else "auto",
                            "times": [x["t"] for x in cur]})
                cur = []
            if w is not None:
                cur.append(w)
    return out


def moment_class(clusters_by_dev, dev, t):
    labs = [c["cls"] for c in clusters_by_dev.get(dev, ()) if any(abs(x - t) <= MATCH_S for x in c["times"])]
    if "attempt" in labs:
        return "attempt"
    if "false" in labs:
        return "false"
    return "unlabelled"


def live_items():
    import kivi_live_wakes as L
    out = []
    for r in L.read_table(ROOT / "live" / "live_wakes.csv"):
        if r["label"] not in ("true", "false") or r["label_source"] not in TRUSTED:
            continue
        d = ROOT / "live" / r["device"] / r["utc"]
        if not (d / "wake_tail.wav").exists():
            continue
        t = L.detection_epoch(r)
        day, eve = madrid(t)
        out.append({"kind": "call_live" if r["label"] == "true" else "false_live", "id": r["run_id"], "device": r["device"],
                    "t": t, "day": day, "evening": eve, "source": r["label_source"] or "no-source",
                    "speaker": r["speaker"] if r["label_source"] in OWNER and r["speaker"] else "unknown",
                    "live_model": r["model"], "dir": str(d), "wake_len": r["wake_len"]})
    return out


def room_items():
    clusters = window_clusters()
    by_dev = defaultdict(list)
    for c in clusters:
        by_dev[c["device"]].append(c)
    items = []
    for c in clusters:
        if c["cls"] in ("attempt", "false"):
            day, eve = madrid(c["t"])
            items.append({"kind": "call_att" if c["cls"] == "attempt" else "false_win", "device": c["device"], "t": c["t"],
                          "day": day, "evening": eve, "speaker": c["speaker"], "source": c["source"]})
    for m in json.loads((KH / "labelq" / "moments.json").read_text())["moments"]:
        day, eve = madrid(m["t"])
        items.append({"kind": "moment", "device": m["device"], "t": m["t"], "day": day, "evening": eve,
                      "cls": moment_class(by_dev, m["device"], m["t"])})
    plan_t = [(c["device"], c["t"]) for c in clusters]      # every window (any label) gets a range: a stable cache
    return items, plan_t, Counter(c["cls"] for c in clusters)


# ---------------------------------------------------------------------------------------------
# room ranges and server-grid embeddings
# ---------------------------------------------------------------------------------------------


def room_pieces():
    import kivi_replay_mine as K
    out = []
    for s in ROOM_SETS:
        if (ROOT / s / "manifest.jsonl").exists():
            out += [dict(p, set=s) for p in K.read_pieces(ROOT, s, bracket="mid")]
    return out


def piece_of(pieces_by_dev, dev, t):
    for p in pieces_by_dev.get(dev, ()):
        if p["start"] <= t <= p["start"] + p["n"] / RATE:
            if p["grid"] != "unknown":
                return p
    return None


def plan_ranges(points, pieces):
    """{range key: (path, s0, s1, src0, clip)} covering [t - PRE_S, t + POST_S] of every point; ranges merged per piece."""
    by_dev = defaultdict(list)
    for p in pieces:
        by_dev[p["device"]].append(p)
    want = defaultdict(list)
    for dev, t in points:
        p = piece_of(by_dev, dev, t)
        if p is None:
            continue
        s0 = max(0, int((t - PRE_S - p["start"]) * RATE))
        s1 = min(p["n"], int((t + POST_S - p["start"]) * RATE))
        want[p["clip"]].append((s0, s1, p))
    out = {}
    for clip, xs in want.items():
        xs.sort(key=lambda x: x[0])
        cur = None
        for s0, s1, p in xs + [(None, None, None)]:
            if cur and (s0 is None or s0 > cur[1] + int(MERGE_GAP_S * RATE)):
                key = hashlib.sha1(f"{clip}|{cur[0]}|{cur[1]}|{cur[2]['src0']}|{WARM_S}".encode()).hexdigest()[:20]
                out[key] = (str(cur[2]["path"]), cur[0], cur[1], cur[2]["src0"], clip, cur[2]["start"], cur[2]["device"])
                cur = None
            if s0 is None:
                break
            cur = [s0, s1, p] if cur is None else [cur[0], max(cur[1], s1), p]
    return out


_W = {}


def embed_range(path, s0, s1, src0):
    """Server-grid embeddings of the steps of piece samples (s0, s1], as kivi_replay_mine.stream_range forms them:
    (sw, n_chunks, embeddings [n, 96] float32, embeddings per chunk)."""
    import kivi_replay_mine as K
    from pyopen_wakeword import OpenWakeWordFeatures
    if "feats" not in _W:
        _W["feats"] = OpenWakeWordFeatures.from_builtin()
    feats = _W["feats"]
    g = K.grid_phase(src0)
    s1 = min(s1, K.piece_samples(path))
    sw = max(g, g + ((s0 - int(WARM_S * RATE) - g) // CHUNK) * CHUNK)
    n_chunks = max(0, (s1 - sw) // CHUNK)
    feats.reset()
    embs, per = [], np.zeros(n_chunks, np.int32)
    with open(path, "rb") as f:
        f.seek(44 + 2 * sw)
        raw = f.read(2 * n_chunks * CHUNK)
    for i in range(n_chunks):
        for e in feats.process_streaming(raw[2 * i * CHUNK:2 * (i + 1) * CHUNK]):
            x = np.asarray(e, np.float32).reshape(-1, 96)
            embs.append(x)
            per[i] += len(x)
    E = np.concatenate(embs) if embs else np.zeros((0, 96), np.float32)
    return sw, n_chunks, E, per


def _embed_task(args):
    key, path, s0, s1, src0 = args
    dst = OUT / "emb" / f"{key}.npz"
    if dst.exists():
        return key, 0.0
    t = time.time()
    sw, n, E, per = embed_range(path, s0, s1, src0)
    tmp = str(dst) + ".part.npz"
    np.savez(tmp, sw=sw, n_chunks=n, E=E, per=per, s0=s0, s1=s1)
    os.replace(tmp, dst)
    os.chmod(dst, 0o600)
    return key, time.time() - t


def score_range(model, z):
    """Detections (piece seconds) of one tflite model on one cached range: exactly kivi_replay_mine.stream_range's
    steps (the model fed the same embeddings in the same order), then the server rule from the range start."""
    model.reset()
    E, sw, n = z["E"], int(z["sw"]), int(z["n_chunks"])
    out = []
    for i in range(len(E)):
        out.extend(model.process_streaming(E[i].reshape(1, 1, 1, 96)))
    times = (sw + (np.arange(n) + 1) * CHUNK) / RATE
    k = min(len(times), len(out))
    times, probs = times[len(times) - k:], np.asarray(out[len(out) - k:], np.float32)
    keep = (times > int(z["s0"]) / RATE + 1e-9) & (times <= int(z["s1"]) / RATE + 1e-9)
    return times[keep], probs[keep]


def _score_task(args):
    keys, specs = args            # specs: [(model key, tflite, raw)]
    from pyopen_wakeword import OpenWakeWord
    if "models" not in _W:
        _W["models"] = {}
    res = {}
    for mk, f, raw in specs:
        if mk not in _W["models"]:
            _W["models"][mk] = OpenWakeWord.from_model(str(f))
    for key in keys:
        z = np.load(OUT / "emb" / f"{key}.npz")
        for mk, f, raw in specs:
            t, p = score_range(_W["models"][mk], z)
            res.setdefault(mk, {})[key] = [round(x, 3) for x in detections(t, p, raw)]
    return res


# ---------------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------------


def plan(a):
    items, points, ccls = room_items()
    pieces = room_pieces()
    ranges = plan_ranges(points + [(it["device"], it["t"]) for it in items], pieces)
    return items, ranges, pieces, ccls


def cmd_embed(a):
    (OUT / "emb").mkdir(parents=True, exist_ok=True)
    for d in (OUT, OUT / "emb"):
        os.chmod(d, 0o700)
    _, ranges, _, _ = plan(a)
    todo = [(k, *v[:4]) for k, v in ranges.items() if not (OUT / "emb" / f"{k}.npz").exists()]
    secs = sum(v[2] - v[1] for v in ranges.values()) / RATE
    print(f"ranges {len(ranges)} ({secs / 3600:.1f} h), to embed {len(todo)}", flush=True)
    t0, done = time.time(), 0
    with mp.get_context("spawn").Pool(a.jobs) as pool:
        for _ in pool.imap_unordered(_embed_task, todo, chunksize=4):
            done += 1
            if done % 500 == 0:
                print(f"  {done}/{len(todo)} {time.time() - t0:.0f} s", flush=True)
    print(f"embedded {done} ranges in {time.time() - t0:.0f} s", flush=True)


def load_dets(mk):
    p = OUT / "dets" / f"{mk}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def cmd_score(a):
    (OUT / "dets").mkdir(parents=True, exist_ok=True)
    (OUT / "live").mkdir(parents=True, exist_ok=True)
    M = models(a.model)
    _, ranges, _, _ = plan(a)
    keys = [k for k in ranges if (OUT / "emb" / f"{k}.npz").exists()]
    have = {m["key"]: load_dets(m["key"]) for m in M.values()}
    specs = [(m["key"], str(m["tflite"]), m["raw"]) for m in M.values()]
    todo = defaultdict(list)                       # range -> models missing it
    for k in keys:
        miss = tuple(s for s in specs if k not in have[s[0]])
        if miss:
            todo[miss].append(k)
    t0 = time.time()
    for miss, ks in todo.items():
        print(f"score {len(ks)} ranges x {len(miss)} models", flush=True)
        batches = [(ks[i:i + 40], list(miss)) for i in range(0, len(ks), 40)]
        with mp.get_context("spawn").Pool(a.jobs) as pool:
            for res in pool.imap_unordered(_score_task, batches):
                for mk, d in res.items():
                    have[mk].update(d)
    for mk, d in have.items():
        p = OUT / "dets" / f"{mk}.json"
        p.write_text(json.dumps(d))
        os.chmod(p, 0o600)
    print(f"room scored in {time.time() - t0:.0f} s", flush=True)
    score_live(M, a.jobs)


def _live_task(args):
    it, specs = args
    import eval_stream as E
    import kivi_fair_fa as F
    if "lfeats" not in _W:
        from pyopen_wakeword import OpenWakeWordFeatures
        _W["lfeats"] = OpenWakeWordFeatures.from_builtin()
        _W["lmodels"] = {}
    for mk, f, raw in specs:
        if mk not in _W["lmodels"]:
            from pyopen_wakeword import OpenWakeWord
            _W["lmodels"][mk] = OpenWakeWord.from_model(str(f))
    F.MODELS = {mk: (f, raw, "") for mk, f, raw in specs}
    d = Path(it["dir"])
    tail = C.read_pcm16(d / "wake_tail.wav")
    stt = C.read_pcm16(d / "stt.wav") if (d / "stt.wav").exists() else np.zeros(0, np.int16)
    sc = F.score_clip(_W["lfeats"], {mk: _W["lmodels"][mk] for mk, _, _ in specs}, tail, stt,
                      E.wake_samples(it["wake_len"] or 0))
    return it["id"], {mk: [v[0], v[2]] for mk, v in sc.items()}


def score_live(M, jobs):
    items = live_items()
    have = {}
    for m in M.values():
        p = OUT / "live" / f"{m['key']}.json"
        have[m["key"]] = json.loads(p.read_text()) if p.exists() else {}
    specs = [(m["key"], str(m["tflite"]), m["raw"]) for m in M.values()]
    tasks = []
    for it in items:
        miss = [s for s in specs if it["id"] not in have[s[0]]]
        if miss:
            tasks.append((it, miss))
    t0 = time.time()
    with mp.get_context("spawn").Pool(jobs) as pool:
        for rid, res in pool.imap_unordered(_live_task, tasks):
            for mk, v in res.items():
                have[mk][rid] = v
    for mk, d in have.items():
        p = OUT / "live" / f"{mk}.json"
        p.write_text(json.dumps(d))
        os.chmod(p, 0o600)
    print(f"live clips scored: {len(tasks)} clips in {time.time() - t0:.0f} s", flush=True)


def synthetic(M):
    import v6_room as R
    import v6_seq_eval as SE
    res = {}
    cat = {}
    with open(KH / DECOY_EVAL[1], newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            cat[r.get("file") or r.get("name") or ""] = r.get("category", "")
    dz = np.load(KH / DECOY_EVAL[0])
    names = [str(x) for x in dz["names"]]
    cats = [cat.get(n.split("|")[0], "?") for n in names]
    for n, m in M.items():
        h = R.Head(m["onnx"])
        out = {}
        for lab, f in C5_SETS:
            mx = SE.clip_max(h, np.load(KH / f))
            out[lab] = [int((mx > m["raw"]).sum()), int(len(mx))]
        mx = SE.clip_max(h, dz)
        out["decoy"] = [int((mx > m["raw"]).sum()), int(len(mx))]
        for c in sorted(set(cats)):
            sel = np.array([x == c for x in cats])
            out[f"decoy:{c}"] = [int((mx[sel] > m["raw"]).sum()), int(sel.sum())]
        res[n] = out
    return res


# ---------------------------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------------------------


def evaluate(M):
    items, ranges, pieces, ccls = plan(None)
    by_dev = defaultdict(list)
    for p in pieces:
        by_dev[p["device"]].append(p)
    piece_ranges = defaultdict(list)                 # clip -> [range key]
    for k, v in ranges.items():
        piece_ranges[v[4]].append(k)
    dets = {n: load_dets(m["key"]) for n, m in M.items()}
    rows, nogrid = [], Counter()
    for it in items:
        p = piece_of(by_dev, it["device"], it["t"])
        if p is None:
            nogrid[f"{it['kind']}|{day_group(it['day'])}"] += 1
            continue
        rel = it["t"] - p["start"]
        hits = {}
        for n in M:
            hits[n] = any(abs(x - rel) <= MATCH_S for k in piece_ranges[p["clip"]] for x in dets[n].get(k, ()))
        rows.append(dict(it, hits=hits))
    live = {n: json.loads((OUT / "live" / f"{m['key']}.json").read_text()) for n, m in M.items()}
    for it in live_items():
        hits = {}
        for n in M:
            v = live[n].get(it["id"])
            if v is None:
                break
            hits[n] = bool(v[1]) if it["kind"] == "call_live" else v[0] is not None
        else:
            rows.append(dict({k: v for k, v in it.items() if k not in ("dir", "wake_len")}, hits=hits))
    return rows, nogrid, ccls


def agg(rows, names, **flt):
    sel = [r for r in rows if all((f(r) if callable(f) else r.get(k) == f) for k, f in flt.items())]
    return {n: [sum(r["hits"][n] for r in sel), len(sel)] for n in names}, len(sel)


def fmt(k, n, ci=True):
    if n == 0:
        return "-"
    lo, hi = clopper(k, n)
    return f"{k}/{n} ({100 * lo:.0f}-{100 * hi:.0f} %)" if ci else f"{k}/{n}"


def mark(M, n, grp):
    s = M[n]["seen"].get(grp, "")
    return f" *{s}*" if s else ""


def cmd_report(a):
    M = models(a.model)
    names = list(M)
    rows, nogrid, ccls = evaluate(M)
    syn = synthetic(M)
    groups = ["TEST"] + sorted({day_group(r["day"]) for r in rows} - {"TEST"})
    is_call = lambda r: r["kind"] in ("call_live", "call_att")             # noqa: E731
    is_false = lambda r: r["kind"] in ("false_live", "false_win")          # noqa: E731
    js = {"generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
          "models": {n: {"file": str(m["tflite"].relative_to(KH)), "raw": m["raw"], "in_sample": m["seen"],
                         "note": m.get("note", "")} for n, m in M.items()},
          "no_server_grid": dict(nogrid), "window_clusters": dict(ccls), "tables": {}, "synthetic": syn}
    L = ["# Kivi model benchmark on the newest labels", "",
         f"Generated {js['generated_utc']} by `kivi_bench.py` (homelab). Labels read at run time; counts only. Each model "
         "at its own raw threshold, server runtime (TFLite) on the server grid. TEST = odd Madrid days from 2026-11-09 "
         "(honest for every model). TRAIN groups: `<=11-08` (2026-11-05 to -08), then each even day. Cells marked *T* "
         "(trained on that day group), *S* (threshold or arm chosen on it) or *T+S* are in-sample for that model and are "
         "never evidence for it. 95 % intervals: Clopper-Pearson.", "",
         "| model | file | raw | in-sample day groups | note |", "| --- | --- | --- | --- | --- |"]
    for n, m in M.items():
        seen = ", ".join(f"{g} {s}" for g, s in m["seen"].items()) or "none recorded"
        L.append(f"| {n} | `{m['tflite'].relative_to(KH)}` | {m['raw']:.4f} | {seen} | {m.get('note', '')} |")
    L += ["", "Selection effects: live clips exist because the live model of the time woke (v5 2026-11-08 to -11, v6.2 "
          "2026-11-12 morning, v6 since 14:44Z) or a button was pressed; room moments and label-queue windows were picked "
          "by v5, v6, v6.2 and p3a-c scores (>= 0.3 / 0.5), so models outside that set (the v6.3r arms) can fire on room "
          "moments nobody picked and that are not counted here.", ""]

    def table(title, cols, cell_rows):
        L.extend([f"#### {title}", "", "| model | " + " | ".join(c for c, _ in cols) + " |",
                  "| --- | " + " | ".join("---" for _ in cols) + " |"])
        for n in names:
            L.append(f"| {n} | " + " | ".join(cell_rows(n, f) for _, f in cols) + " |")
        L.append("")

    for grp in groups:
        side = "TEST (odd days from 2026-11-09)" if grp == "TEST" else f"TRAIN day group {grp}"
        L += [f"## {side}", ""]
        g = (lambda r, grp=grp: day_group(r["day"]) == grp)
        # (a) calls
        cols = []
        for dev in ("c3-a", "c3-b"):
            cols.append((f"{dev} live", dict(group=g, kind="call_live", device=dev)))
            cols.append((f"{dev} attempts", dict(group=g, kind="call_att", device=dev)))
        cols.append(("all calls", dict(group=g, call=is_call)))
        res = {c: agg(rows, names, **f)[0] for c, f in cols}
        js["tables"][f"{grp}|calls"] = res
        table(f"(a) Calls detected / total ({side})", [(c, c) for c, _ in cols],
              lambda n, c, grp=grp: fmt(*res[c][n], ci=(c == "all calls")) + (mark(M, n, grp) if c == "all calls" else ""))
        spk = sorted({r["speaker"] for r in rows if is_call(r) and g(r)})
        if spk:
            res_s = {s: agg(rows, names, group=g, call=is_call, speaker=s)[0] for s in spk}
            js["tables"][f"{grp}|calls_speaker"] = res_s
            table(f"(a) Calls per speaker (owner labels only) ({side})", [(s, s) for s in spk],
                  lambda n, s: fmt(*res_s[s][n], ci=False))
        # (b) false material
        cols = []
        for dev in ("c3-a", "c3-b"):
            cols.append((f"{dev} live", dict(group=g, kind="false_live", device=dev)))
            cols.append((f"{dev} windows", dict(group=g, kind="false_win", device=dev)))
            cols.append((f"{dev} evening", dict(group=g, false=is_false, device=dev, evening=True)))
        cols.append(("all", dict(group=g, false=is_false)))
        res_b = {c: agg(rows, names, **f)[0] for c, f in cols}
        js["tables"][f"{grp}|false"] = res_b
        table(f"(b) False material fired / total ({side})", [(c, c) for c, _ in cols],
              lambda n, c, grp=grp: fmt(*res_b[c][n], ci=(c in ("all", "c3-b evening"))) +
              (mark(M, n, grp) if c == "all" else ""))
        eves = sorted({(r["device"], r["day"]) for r in rows if is_false(r) and r["evening"] and g(r)})
        if eves:
            res_e = {f"{d} {day}": agg(rows, names, group=g, false=is_false, device=d, day=day, evening=True)[0]
                     for d, day in eves}
            js["tables"][f"{grp}|false_evening"] = res_e
            L += [f"#### (b) False material per evening (18-24 Madrid), fired / total ({side})", "",
                  "| TV day | " + " | ".join(names) + " |", "| --- | " + " | ".join("---" for _ in names) + " |"]
            for e, v in res_e.items():
                L.append(f"| {e} | " + " | ".join(fmt(*v[n], ci=False) for n in names) + " |")
            L.append("")
        # (c) room moments
        cols = [(f"{dev} {cls}", dict(group=g, kind="moment", device=dev, cls=cls))
                for dev in ("c3-a", "c3-b") for cls in ("false", "attempt", "unlabelled")]
        res_c = {c: agg(rows, names, **f)[0] for c, f in cols}
        js["tables"][f"{grp}|moments"] = res_c
        table(f"(c) Room moments of labelq/moments.json fired / total, by their labels ({side})",
              [(c, c) for c, _ in cols], lambda n, c, grp=grp: fmt(*res_c[c][n], ci=False) +
              (mark(M, n, grp) if c == "c3-b unlabelled" else ""))
    # (d) synthetic
    L += ["## (d) Synthetic sets (no day; held-out voices)", "",
          "C5: accept rate of the held-out-voice positives (higher is better). Decoys: the 787 evaluation clips of "
          "`v6p3r_decoys.py` (\"oye\" + another word, \"oye\" + a near word, a near word in a sentence; held-out voices), "
          "share of clips the model fires on (lower is better). The v6.3r arms trained on the decoy training clips "
          "(other voices); per clip maximum of the ONNX head (v6_seq_eval.py).", "",
          "| model | C5 clean | C5 TV 10 dB | C5 quiet | decoys all | " +
          " | ".join(k.split(":", 1)[1] for k in sorted(syn[names[0]]) if k.startswith("decoy:")) + " |",
          "| --- | --- | --- | --- | --- | " + " | ".join("---" for k in syn[names[0]] if k.startswith("decoy:")) + " |"]
    for n in names:
        s = syn[n]
        L.append(f"| {n} | {fmt(*s['clean'])} | {fmt(*s['tv10'])} | {fmt(*s['quiet'])} | {fmt(*s['decoy'])} | " +
                 " | ".join(fmt(*s[k], ci=False) for k in sorted(s) if k.startswith("decoy:")) + " |")
    L += ["", "## Coverage", "",
          f"Window clusters (all tables, unique per TV within 3 s) by current label: {dict(ccls)}. Items without "
          f"server-grid room audio (pieces of 2026-11-05/-06 have no derived grid; 2026-11-08 before 13:30Z has no copy): "
          f"{dict(sorted(nogrid.items()))}.", ""]
    (OUT / "BENCH.md").write_text("\n".join(L))
    (OUT / "bench.json").write_text(json.dumps(js, indent=1))
    for p in (OUT / "BENCH.md", OUT / "bench.json"):
        os.chmod(p, 0o600)
    print("\n".join(L))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["embed", "score", "report", "run"])
    ap.add_argument("--jobs", type=int, default=7)
    ap.add_argument("--model", action="append", default=[], metavar="NAME=FILE_STEM:RAW")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    os.chmod(OUT, 0o700)
    if a.what in ("embed", "run"):
        cmd_embed(a)
    if a.what in ("score", "run"):
        cmd_score(a)
    if a.what in ("report", "run"):
        cmd_report(a)


if __name__ == "__main__":
    main()
