#!/usr/bin/env python3
"""Replay of the v5-era room audio (the copy of `kivi_negatives.py --out-name negatives-v5era`) through kivi (v1),
oye_kivi (v4) and oye_kivi_next (v5) at once, and the missed-wake mining on top of it.

    kivi_replay_mine.py score --bench-s 300 --jobs 6      # speed of the pass on a few minutes (nothing saved)
    kivi_replay_mine.py score --jobs 6                    # the one streaming pass; resumable (chunk files)
    kivi_replay_mine.py report                            # replay false wakes: per device / part, per hour and evening
    kivi_replay_mine.py selfcheck                         # the live wake tails score above threshold (feature / level check)
    kivi_replay_mine.py candidates                        # ranked possible MISSED "oye Kivi" wakes -> live-misses/

Never plays, transcribes or prints audio content: scores, times, levels and counts only.

Day split (owner decision 2026-11-11): alternating Europe/Madrid local days, `test` on 2026-11-09, -11, ... and
`mine` on 2026-11-08, -10, ... (`kivi_real_common.day_role`). Test-day audio may be scored and labelled for
evaluation but is never used to choose models or thresholds. The pieces carry the part in the manifest `split`.

The pass is server-exact: each piece is streamed from its sample 0 with pyopen-wakeword in 1280-sample chunks
(`eval_stream.stream` semantics, never batched). A long piece is cut into time ranges that run in parallel, each
started `--warm-s` earlier on a chunk boundary and its warm-up outputs dropped; the state of the runtime is a few
seconds of audio, so the scores equal the continuous pass (tested). Per range only the steps where some model
scores above 0.3 are kept (`scores/v5era-replay/chunks/*.npz`: times and three scores, no audio); `report` and
`candidates` work from them. Detections use the server's rule: probability above the raw threshold, 2 s refractory.
"""
import os

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import argparse  # noqa: E402
import csv  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from collections import Counter, defaultdict  # noqa: E402
from datetime import datetime, time as dtime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import numpy as np  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kivi_extract as X  # noqa: E402
import kivi_live_wakes as L  # noqa: E402
import kivi_real_common as C  # noqa: E402

RATE = C.RATE
CHUNK = 1280
WORK = C.kivi_home()
MODELS_DIR = C.kivi_models_repo() / "wakeword"
MODEL_FILES = {
    "kivi": MODELS_DIR / "kivi/v1-2026-11-05/kivi_uncalibrated.tflite",
    "oye_kivi": MODELS_DIR / "oye_kivi/v4-oye-kivi-2026-11-07/oye_kivi_uncalibrated.tflite",
    "oye_kivi_next": MODELS_DIR / "oye_kivi_next/v5-oye-kivi-2026-11-08/oye_kivi_next_uncalibrated.tflite",
}
NAMES = tuple(MODEL_FILES)                                 # column order of every score array
SHORT = {"kivi": "v1", "oye_kivi": "v4", "oye_kivi_next": "v5"}
SERVER_RAW = {"kivi": 0.98, "oye_kivi": 0.9437, "oye_kivi_next": 0.9754}   # raw point where the served file is 0.5
THRESHOLDS = (0.9, 0.9437, 0.95, 0.9754, 0.98, 0.99)
REFRACTORY_S = 2.0
KEEP_ABOVE = 0.3                                           # steps kept: some model above this
MERGE_S = 1.5
EVENING = (18, 24)                                         # local hours (Europe/Madrid)
DEFAULT_OUT = "scores/v5era-replay"
RETRY_S = 30.0
CAND_MIN_S = 5.0                                           # another event closer than this is the same utterance, not a retry
MAX_CANDIDATES = 40
MIN_SEP_S = 10.0
WINDOW_S = 6.0
FA_KEYS = ["v4@0.9437", "v5@0.9754", "v5@0.95", "v5@0.98", "v1@0.98"]


# ---------------------------------------------------------------------------------------------
# streaming one range of a piece
# ---------------------------------------------------------------------------------------------

_W = {}


def load_runtime():
    from pyopen_wakeword import OpenWakeWord, OpenWakeWordFeatures
    feats = OpenWakeWordFeatures.from_builtin()
    return feats, {n: OpenWakeWord.from_model(str(p)) for n, p in MODEL_FILES.items()}


def piece_samples(path):
    return max(0, (os.path.getsize(path) - 44) // 2)


def stream_range(feats, models, path, s0, s1, warm_s=60.0, block_s=60):
    """Scores of the steps whose end time lies in (s0, s1] samples of the piece, started warm_s earlier on a chunk
    boundary: (times in s since the piece start, float32 array [steps, models]). Audio is read one block at a time."""
    s0 = (s0 // CHUNK) * CHUNK
    sw = max(0, s0 - (int(warm_s * RATE) // CHUNK) * CHUNK)
    s1 = min(s1, piece_samples(path))
    n_chunks = (s1 - sw) // CHUNK
    if n_chunks <= 0:
        return np.zeros(0), np.zeros((0, len(NAMES)), np.float32)
    feats.reset()
    for m in models.values():
        m.reset()
    out = {n: [] for n in NAMES}
    block = block_s * RATE // CHUNK * CHUNK
    pos, end = sw, sw + n_chunks * CHUNK
    with open(path, "rb") as f:
        while pos < end:
            cnt = min(block, end - pos)
            f.seek(44 + 2 * pos)
            raw = f.read(2 * cnt)
            if len(raw) < 2 * cnt:
                raise SystemExit(f"{path}: short read at sample {pos}")
            for k in range(0, len(raw), 2 * CHUNK):
                for emb in feats.process_streaming(raw[k:k + 2 * CHUNK]):
                    for n, m in models.items():
                        out[n].extend(m.process_streaming(emb))
            pos += cnt
    times = (sw + (np.arange(n_chunks) + 1) * CHUNK) / RATE
    n_steps = min([len(times)] + [len(v) for v in out.values()])
    probs = np.stack([np.asarray(out[n][len(out[n]) - n_steps:], np.float32) for n in NAMES], axis=1)
    times = times[len(times) - n_steps:]
    keep = (times > s0 / RATE + 1e-9) & (times <= s1 / RATE + 1e-9)
    return times[keep], probs[keep]


def _init_worker():
    _W["rt"] = load_runtime()


def _task(args):
    """(clip_key, path, s0, s1, warm_s, dst or None) -> (clip_key, s0, n_steps, seconds)."""
    key, path, s0, s1, warm_s, dst = args
    if "rt" not in _W:
        _init_worker()
    t = time.time()
    times, probs = stream_range(*_W["rt"], path, s0, s1, warm_s)
    if dst:
        sel = (probs > KEEP_ABOVE).any(axis=1)
        tmp = str(dst) + ".part"
        with open(tmp, "wb") as f:
            np.savez_compressed(f, t=times[sel].astype(np.float64), p=probs[sel], steps=len(times), s0=s0, s1=s1)
        os.replace(tmp, dst)
    return key, s0, len(times), time.time() - t


# ---------------------------------------------------------------------------------------------
# pieces, tasks, aggregation (pure)
# ---------------------------------------------------------------------------------------------


def read_pieces(root, out_name="negatives-v5era"):
    """[{clip, path, device, part, start (epoch), n}] of the copied set."""
    root = Path(root)
    rows = []
    for r in C.read_jsonl(root / out_name / "manifest.jsonl"):
        p = root / r["clip"]
        if not p.exists():
            continue
        rows.append({"clip": r["clip"], "path": p, "device": r["device"], "part": r["split"],
                     "start": X.parse_utc(r["start_utc"]).timestamp(), "n": piece_samples(p)})
    rows.sort(key=lambda r: (r["device"], r["start"]))
    return rows


def plan_tasks(pieces, chunk_s=1200.0):
    """[(clip, path, s0, s1)] of every piece; range edges are multiples of 1280 samples."""
    step = max(1, int(chunk_s * RATE) // CHUNK) * CHUNK
    return [(pc["clip"], str(pc["path"]), s0, min(pc["n"], s0 + step)) for pc in pieces for s0 in range(0, pc["n"], step)]


def chunk_name(clip, s0):
    return hashlib.sha1(f"{clip}|{s0}".encode()).hexdigest()[:20] + ".npz"


def load_piece_steps(chunks_dir, clip, tasks_of_clip):
    """Steps above KEEP_ABOVE of a piece: (times, probs[steps, models]) sorted by time, plus the step count."""
    ts, ps, steps = [], [], 0
    for _, _, s0, _ in tasks_of_clip:
        z = np.load(Path(chunks_dir) / chunk_name(clip, s0))
        ts.append(z["t"])
        ps.append(z["p"])
        steps += int(z["steps"])
    if not ts:
        return np.zeros(0), np.zeros((0, len(NAMES)), np.float32), 0
    t, p = np.concatenate(ts), np.concatenate(ps)
    o = np.argsort(t, kind="stable")
    return t[o], p[o], steps


def detections_of(times, probs, thr, refractory=REFRACTORY_S):
    """The server's rule on one model's step scores: above thr, then `refractory` seconds of silence."""
    out, last = [], -1e9
    for i in np.nonzero(probs > thr)[0]:
        if times[i] - last >= refractory:
            out.append(float(times[i]))
            last = times[i]
    return out


def cluster_events(times, probs, merge_s=MERGE_S, floor=KEEP_ABOVE):
    """Events: steps above `floor` for any model, chained while the gap is at most `merge_s`. Per event the start,
    end, per-model maximum and the time of each maximum, and `t` = the time of the v5 maximum (else of the highest)."""
    idx = np.nonzero((probs > floor).any(axis=1))[0]
    if not len(idx):
        return []
    groups, cur = [], [idx[0]]
    for i in idx[1:]:
        if times[i] - times[cur[-1]] <= merge_s:
            cur.append(i)
        else:
            groups.append(cur)
            cur = [i]
    groups.append(cur)
    j5 = NAMES.index("oye_kivi_next")
    ev = []
    for g in groups:
        pg, tg = probs[g], times[g]
        mx = pg.max(axis=0)
        tm = [float(tg[int(pg[:, k].argmax())]) for k in range(len(NAMES))]
        lead = j5 if mx[j5] > floor else int(mx.argmax())
        ev.append({"t0": float(tg[0]), "t1": float(tg[-1]), "t": tm[lead], "peak": [float(x) for x in mx], "tpeak": tm})
    return ev


def local_overlap_hours(t0, t1, hours=EVENING):
    """Hours of [t0, t1] (epoch) that fall between local `hours[0]` and `hours[1]` o'clock (Europe/Madrid)."""
    z = ZoneInfo(C.DAY_TZ)
    d = datetime.fromtimestamp(t0, z).date()
    tot = 0.0
    while True:
        a = datetime.combine(d, dtime(hours[0]), z).timestamp()
        b = datetime.combine(d + timedelta(days=1), dtime(0), z).timestamp() if hours[1] == 24 else \
            datetime.combine(d, dtime(hours[1]), z).timestamp()
        if a >= t1:
            break
        tot += max(0.0, min(b, t1) - max(a, t0))
        d += timedelta(days=1)
    return tot / 3600


def local_date(t):
    return datetime.fromtimestamp(t, ZoneInfo(C.DAY_TZ)).date().isoformat()


def local_hour(t):
    return datetime.fromtimestamp(t, ZoneInfo(C.DAY_TZ)).hour


# ---------------------------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------------------------


def model_info():
    return {n: {"path": str(p), "sha256": L.sha256_file(p), "version": p.parent.name, "server_0_5_raw": SERVER_RAW[n]}
            for n, p in MODEL_FILES.items()}


def _bench_tasks(pieces, jobs, bench_s, warm_s):
    out = []
    for p in sorted(pieces, key=lambda p: -p["n"])[:jobs]:
        s0 = min(p["n"] // 2 // CHUNK * CHUNK, 600 * RATE // CHUNK * CHUNK)
        out.append((p["clip"], str(p["path"]), s0, min(p["n"], s0 + int(bench_s * RATE)), warm_s, None))
    return out


def cmd_score(a, out=print):
    import multiprocessing as mp
    root = Path(a.dataset_root).expanduser()
    pieces = read_pieces(root, a.set)
    if not pieces:
        raise SystemExit(f"no pieces under {root / a.set}")
    tasks = plan_tasks(pieces, a.chunk_s)
    hours = sum(p["n"] for p in pieces) / RATE / 3600
    out(f"{len(pieces)} pieces, {hours:.2f} h, {len(tasks)} ranges of {a.chunk_s:.0f} s (+{a.warm_s:.0f} s warm-up), {a.jobs} worker(s)")
    if a.bench_s:
        bt = _bench_tasks(pieces, a.jobs, a.bench_s, a.warm_s)
        with mp.get_context("spawn").Pool(a.jobs, initializer=_init_worker) as pool:
            pool.map(_task, [(f"w{i}", bt[0][1], 0, 4 * CHUNK, 0.0, None) for i in range(a.jobs)])      # models loaded
            t = time.time()
            res = pool.map(_task, bt)
            wall = time.time() - t
        audio = sum(r[2] * CHUNK / RATE for r in res)
        rtf = audio / wall
        est_h = hours * 3600 / rtf / 3600 * (1 + a.warm_s / a.chunk_s)
        out(f"bench: {audio:.0f} s of audio in {wall:.1f} s wall with {a.jobs} worker(s) = {rtf:.1f}x real time; "
            f"the full pass would take about {est_h:.2f} h wall")
        return {"rtf": rtf, "est_wall_h": est_h}
    sdir = root / a.out
    for d in (sdir, sdir / "chunks"):
        if not d.exists():
            C.private_mkdir(d, root)
    chunks = sdir / "chunks"
    todo = [(c, p, s0, s1, a.warm_s, str(chunks / chunk_name(c, s0))) for c, p, s0, s1 in tasks if not (chunks / chunk_name(c, s0)).exists()]
    out(f"{len(tasks) - len(todo)} range(s) already scored, {len(todo)} to do")
    t0, done, audio_done = time.time(), 0, 0.0
    with mp.get_context("spawn").Pool(a.jobs, initializer=_init_worker) as pool:
        for _key, _s0, n, _sec in pool.imap_unordered(_task, todo):
            done += 1
            audio_done += n * CHUNK / RATE
            if done % 10 == 0 or done == len(todo):
                el = time.time() - t0
                out(f"{done}/{len(todo)} ranges, {audio_done / 3600:.1f} h of audio, {el / 60:.1f} min elapsed, "
                    f"eta {el / done * (len(todo) - done) / 60:.1f} min")
    meta = {"models": model_info(), "keep_above": KEEP_ABOVE, "warm_s": a.warm_s, "chunk_s": a.chunk_s, "pieces": len(pieces),
            "hours": round(hours, 3), "scored_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "wall_min_last_run": round((time.time() - t0) / 60, 1)}
    (sdir / "score_meta.json").write_text(json.dumps(meta, indent=1))
    os.chmod(sdir / "score_meta.json", 0o600)
    out(f"pass finished in {(time.time() - t0) / 60:.1f} min wall")
    return meta


# ---------------------------------------------------------------------------------------------
# events and replay false wakes
# ---------------------------------------------------------------------------------------------


def collect(root, set_name="negatives-v5era", out_dir=DEFAULT_OUT, chunk_s=1200.0):
    """Per piece: its events and detections, built from the chunk files. Exits if a range is missing."""
    root = Path(root)
    pieces = read_pieces(root, set_name)
    tasks = plan_tasks(pieces, chunk_s)
    by_clip = defaultdict(list)
    for t in tasks:
        by_clip[t[0]].append(t)
    chunks = root / out_dir / "chunks"
    missing = [t for t in tasks if not (chunks / chunk_name(t[0], t[2])).exists()]
    if missing:
        raise SystemExit(f"{len(missing)} range(s) not scored yet: run `score` first")
    res = []
    for pc in pieces:
        t, p, steps = load_piece_steps(chunks, pc["clip"], by_clip[pc["clip"]])
        dets = {f"{SHORT[n]}@{thr}": [pc["start"] + x for x in detections_of(t, p[:, k], thr)]
                for k, n in enumerate(NAMES) for thr in THRESHOLDS}
        evs = cluster_events(t, p)
        for e in evs:
            for key in ("t0", "t1", "t"):
                e[key + "_abs"] = pc["start"] + e[key]
            e["tpeak_abs"] = [pc["start"] + x for x in e["tpeak"]]
        res.append({"piece": pc, "steps": steps, "events": evs, "dets": dets})
    return res


def poisson(k, h):
    from eval_stream import poisson_ci
    return poisson_ci(k, h) if h > 0 else (0.0, float("nan"))


def fa_table(res, keys=FA_KEYS):
    """{(device|'all', part|'all'): {hours, evening_hours, pieces, k: Counter, k_eve: Counter}}."""
    cells = defaultdict(lambda: {"hours": 0.0, "evening_hours": 0.0, "pieces": 0, "k": Counter(), "k_eve": Counter()})
    for r in res:
        pc = r["piece"]
        h = pc["n"] / RATE / 3600
        eh = local_overlap_hours(pc["start"], pc["start"] + pc["n"] / RATE)
        for scope in ((pc["device"], pc["part"]), (pc["device"], "all"), ("all", pc["part"]), ("all", "all")):
            c = cells[scope]
            c["hours"] += h
            c["evening_hours"] += eh
            c["pieces"] += 1
            for key in keys:
                ts = r["dets"][key]
                c["k"][key] += len(ts)
                c["k_eve"][key] += sum(1 for t in ts if EVENING[0] <= local_hour(t) < EVENING[1])
    return cells


def fmt_rate(k, h):
    if h <= 0:
        return "n/a"
    lo, hi = poisson(k, h)
    return f"{k / h:.3f} [{lo:.3f}, {hi:.3f}]"


def report_markdown(res, keys=FA_KEYS):
    cells = fa_table(res, keys)
    rows = ["| device | part | hours | evening h | model@raw | k | per hour [95% CI] | k evening | per evening hour [95% CI] |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for scope in sorted(cells, key=lambda s: (s[0] == "all", s[0], s[1] == "all", s[1])):
        c = cells[scope]
        for key in keys:
            rows.append(f"| {scope[0]} | {scope[1]} | {c['hours']:.2f} | {c['evening_hours']:.2f} | {key} | {c['k'][key]} | "
                        f"{fmt_rate(c['k'][key], c['hours'])} | {c['k_eve'][key]} | {fmt_rate(c['k_eve'][key], c['evening_hours'])} |")
    return "\n".join(rows) + "\n", cells


def per_day_hours(res):
    d = defaultdict(float)
    for r in res:
        pc = r["piece"]
        d[(pc["device"], local_date(pc["start"]), pc["part"])] += pc["n"] / RATE / 3600
    return d


def write_csv(path, rows, fields=None):
    """Atomic rewrite (temp file, fsync, rename), mode 600."""
    p = Path(path)
    fields = fields or list(rows[0].keys())
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def cmd_report(a, out=print):
    root = Path(a.dataset_root).expanduser()
    res = collect(root, a.set, a.out, a.chunk_s)
    md, cells = report_markdown(res)
    sdir = root / a.out
    meta = json.loads((sdir / "score_meta.json").read_text()) if (sdir / "score_meta.json").exists() else {}
    all_keys = list(res[0]["dets"]) if res else []
    live = Counter((r["device"], r["model"]) for r in L.read_table(root / "live" / "live_wakes.csv"))
    summary = {"meta": meta, "refractory_s": REFRACTORY_S,
               "cells": {f"{s[0]}|{s[1]}": {"hours": round(c["hours"], 3), "evening_hours": round(c["evening_hours"], 3),
                                            "k": dict(c["k"]), "k_evening": dict(c["k_eve"])} for s, c in cells.items()},
               "detections_all_thresholds": {f"{s[0]}|{s[1]}": {k: sum(len(r["dets"][k]) for r in res
                                                                       if s[0] in ("all", r["piece"]["device"]) and s[1] in ("all", r["piece"]["part"]))
                                                                  for k in all_keys} for s in cells},
               "hours_by_device_day_part": {f"{k[0]} {k[1]} {k[2]}": round(v, 3) for k, v in sorted(per_day_hours(res).items())},
               "events_total": sum(len(r["events"]) for r in res),
               "live_rows_in_live_wakes_csv": {f"{k[0]} {k[1]}": v for k, v in live.items()}}
    rows = []
    for r in res:
        pc = r["piece"]
        for e in r["events"]:
            rows.append({"device": pc["device"], "part": pc["part"], "clip": pc["clip"], "utc": L.dir_stamp(e["t_abs"]),
                         "t_piece_s": round(e["t"], 2), **{f"peak_{SHORT[n]}": round(e["peak"][k], 4) for k, n in enumerate(NAMES)}})
    rows.sort(key=lambda x: (x["utc"], x["device"]))
    if rows:
        write_csv(sdir / "events.csv", rows)
    (sdir / "fa_summary.json").write_text(json.dumps(summary, indent=1))
    (sdir / "fa_table.md").write_text(md)
    for f in ("fa_summary.json", "fa_table.md"):
        os.chmod(sdir / f, 0o600)
    out(md)
    out(f"events (any model above {KEEP_ABOVE}, merged within {MERGE_S} s): {len(rows)}; files in {sdir}")
    return summary


# ---------------------------------------------------------------------------------------------
# self-check on the live wake tails
# ---------------------------------------------------------------------------------------------


def cmd_selfcheck(a, out=print):
    """Scores the 6 s tails of the live detections (kivi_live_wakes.py extract) with the same runtime; counts only.
    A live detection of model M should show a peak of M above its raw point in the last 2.5 s of the tail."""
    root = Path(a.dataset_root).expanduser()
    feats, models = load_runtime()
    res, peaks = Counter(), defaultdict(list)
    for r in L.read_table(root / "live" / "live_wakes.csv"):
        wav = root / "live" / r["device"] / r["utc"] / "wake_tail.wav"
        if not wav.exists() or r["model"] not in NAMES:
            continue
        t, p = stream_range(feats, models, wav, 0, piece_samples(wav), warm_s=0)
        k = NAMES.index(r["model"])
        last = t > t[-1] - 2.5
        pk = float(p[last, k].max()) if last.any() else float("nan")
        peaks[r["model"]].append(pk)
        res[(r["model"], "n")] += 1
        res[(r["model"], "above_raw_point")] += int(pk > SERVER_RAW[r["model"]])
        res[(r["model"], "above_0.9")] += int(pk > 0.9)
    for m in sorted(peaks):
        out(f"{m}: {res[(m, 'n')]} live tails; peak of the live model in the last 2.5 s above its raw point {SERVER_RAW[m]}: "
            f"{res[(m, 'above_raw_point')]}; above 0.9: {res[(m, 'above_0.9')]}; median peak {np.nanmedian(peaks[m]):.3f}")
    return res


# ---------------------------------------------------------------------------------------------
# missed-wake candidates
# ---------------------------------------------------------------------------------------------


def rank_score(peak, retry_kind):
    """Higher = more worth a listen: v5 peak (closeness to its raw point) + 0.25 v4 + 0.10 v1 (agreement) + retry
    evidence (a live detection or an stt-only run after it +0.5, another candidate 5 to 30 s later +0.1)."""
    v1, v4, v5 = peak
    return round(v5 + 0.25 * v4 + 0.10 * v1 + {"live": 0.5, "stt-only": 0.5, "cand": 0.1}.get(retry_kind, 0.0), 4)


def retry_evidence(cand, pool, live_times, stt_times, window=RETRY_S):
    """What follows the candidate within `window` s on the same device: a live detection, an stt-only run
    (push-to-talk / mic button) or another event of the pool 5 to 30 s later. {"kind", "dt_s", "detail"}."""
    t, dev = cand["t_abs"], cand["device"]
    found = [("live", x - t) for x in live_times.get(dev, ()) if 0 < x - t <= window]
    found += [("stt-only", x - t) for x in stt_times.get(dev, ()) if 0 < x - t <= window]
    found += [("cand", o["t_abs"] - t) for o in pool if o is not cand and o["device"] == dev and CAND_MIN_S <= o["t_abs"] - t <= window]
    if not found:
        return {"kind": "", "dt_s": "", "detail": ""}
    order = {"live": 0, "stt-only": 1, "cand": 2}
    found.sort(key=lambda f: (order[f[0]], f[1]))
    return {"kind": found[0][0], "dt_s": round(found[0][1], 1), "detail": ";".join(f"{k}+{d:.1f}s" for k, d in found[:4])}


def select_candidates(pool, limit=MAX_CANDIDATES, min_sep_s=MIN_SEP_S):
    """Round-robin over (part, device) cells, highest rank first within a cell; a candidate must be `min_sep_s`
    from a chosen one on the same device. A cell that runs out leaves its turns to the others."""
    cells = defaultdict(list)
    for c in pool:
        cells[(c["part"], c["device"])].append(c)
    for v in cells.values():
        v.sort(key=lambda c: -c["rank"])
    chosen = []
    while len(chosen) < limit and any(cells.values()):
        for key in sorted(cells):
            q = cells[key]
            while q and any(c["device"] == q[0]["device"] and abs(q[0]["t_abs"] - c["t_abs"]) < min_sep_s for c in chosen):
                q.pop(0)
            if q and len(chosen) < limit:
                chosen.append(q.pop(0))
    return chosen


def stt_only_times(fetcher, devices_prefix, boot=None):
    """{device: [start epoch]} of the stt-only runs (no wake file in the run directory): push-to-talk, mic button
    and continued conversations. Listing only."""
    boot = L.epoch(L.BOOT_UTC) if boot is None else boot
    out, warn = {}, Counter()
    for dev, prefix in devices_prefix.items():
        wakes = {(f["pipeline"], f["run"]) for f in X.list_files(fetcher, prefix, "wake")}
        out[dev] = sorted(L.run_start(f, boot, warn) for f in X.list_files(fetcher, prefix, "stt")
                          if (f["pipeline"], f["run"]) not in wakes)
    return out


def live_detection_times(root):
    """{device: [epoch]}: the end of the wake file of every live detection run in live_wakes.csv."""
    out = defaultdict(list)
    for r in L.read_table(Path(root) / "live" / "live_wakes.csv"):
        t = datetime.strptime(r["utc"], "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
        out[r["device"]].append(t + float(r["wake_len"] or 0))
    return {d: sorted(v) for d, v in out.items()}


def window_bounds(t_peak, piece_start, n_samples, win_s=WINDOW_S):
    """(first, last) sample of a `win_s` window centred on the peak, shifted to stay inside the piece."""
    w = int(win_s * RATE)
    first = max(0, min(int(round((t_peak - piece_start) * RATE)) - w // 2, n_samples - w))
    return first, min(n_samples, first + w)


def wav_levels(x):
    f = x.astype(np.float64) / 32768
    db = lambda v: round(20 * np.log10(max(v, 1e-9)), 1)   # noqa: E731
    return db(float(np.sqrt((f ** 2).mean())) if len(f) else 0.0), db(float(np.abs(f).max()) if len(f) else 0.0)


def cut_window(path, first, last, dst):
    """Samples [first, last) of a copied piece as a new WAV; returns (rms_dbfs, peak_dbfs)."""
    x = np.fromfile(path, dtype="<i2", count=last - first, offset=44 + 2 * first)
    C.write_wav_new(dst, x)
    return wav_levels(x)


MISS_FIELDS = L.MISS_FIELDS


def candidate_pool(res, floor, v5_point=SERVER_RAW["oye_kivi_next"]):
    """Events where v5 did not fire in the replay (peak at or below its raw point) but v4 or v5 peaks at `floor` or more.
    The old single-word model (v1) alone is not a candidate for "oye Kivi" (it chatters: 12k events above 0.5); it counts
    as agreement in the rank."""
    pool = []
    for r in res:
        pc = r["piece"]
        for e in r["events"]:
            if e["peak"][2] > v5_point or max(e["peak"][1:]) < floor:
                continue
            pool.append({"device": pc["device"], "part": pc["part"], "piece": pc, "peak": e["peak"], "t_abs": e["t_abs"]})
    return pool


def rank_pool(pool, live_t, stt_t):
    for c in pool:
        c["retry"] = retry_evidence(c, pool, live_t, stt_t)
        c["rank"] = rank_score(c["peak"], c["retry"]["kind"])
    return pool


def cmd_candidates(a, fetcher=None, out=print):
    root = Path(a.dataset_root).expanduser()
    res = collect(root, a.set, a.out, a.chunk_s)
    pool = rank_pool(candidate_pool(res, a.floor), live_detection_times(root),
                     stt_only_times(fetcher or L.get_fetcher(a), L.devices_prefix(a)))
    chosen = select_candidates(pool, a.limit)
    out(f"pool (v5 at or below its raw point {SERVER_RAW['oye_kivi_next']}, v4 or v5 peak >= {a.floor}): {len(pool)} event(s); "
        + ", ".join(f"{k[0]}/{k[1]} {v}" for k, v in sorted(Counter((c["part"], c["device"]) for c in pool).items())))
    out("pool by retry evidence: " + ", ".join(f"{k or 'none'} {v}" for k, v in sorted(Counter(c["retry"]["kind"] for c in pool).items())))
    misses = root / "live-misses"
    if not misses.exists():
        C.private_mkdir(misses, root)
    table = misses / "miss_candidates.csv"
    if table.exists() and not a.rewrite:
        raise SystemExit(f"{table} exists (it may hold labels): not rewritten. Use --rewrite only when nothing is labelled.")
    chosen.sort(key=lambda c: -c["rank"])
    rows = []
    for i, c in enumerate(chosen, 1):
        pc = c["piece"]
        utc = L.dir_stamp(c["t_abs"])
        C.private_mkdir(misses / pc["part"] / pc["device"] / utc, root)
        first, last = window_bounds(c["t_abs"], pc["start"], pc["n"])
        rel = f"{pc['part']}/{pc['device']}/{utc}/window.wav"
        if (misses / rel).exists():
            rms, pk = wav_levels(np.fromfile(misses / rel, dtype="<i2", offset=44))
        else:
            rms, pk = cut_window(pc["path"], first, last, misses / rel)
        rows.append({"id": f"mc{i:03d}", "device": pc["device"], "part": pc["part"], "utc": utc,
                     "peak_v1": round(c["peak"][0], 4), "peak_v4": round(c["peak"][1], 4), "peak_v5": round(c["peak"][2], 4),
                     "retry": c["retry"]["kind"], "retry_detail": c["retry"]["detail"], "rank": c["rank"], "label": "", "speaker": "",
                     "level_dbfs": rms, "peak_dbfs": pk, "clip": rel})
    write_csv(table, rows, MISS_FIELDS)
    left = [c for c in pool if c not in chosen]
    out(f"{len(rows)} candidate(s) cut: " + ", ".join(f"{k[0]}/{k[1]} {v}" for k, v in sorted(Counter((r['part'], r['device']) for r in rows).items())))
    out("selected by retry evidence: " + ", ".join(f"{k or 'none'} {v}" for k, v in sorted(Counter(r["retry"] for r in rows).items())))
    out(f"{len(left)} more in the pool beyond the {len(rows)}; lowest rank taken {rows[-1]['rank'] if rows else None}, best left out "
        f"{max((c['rank'] for c in left), default=None)}; left out with v5 peak >= 0.9: {sum(1 for c in left if c['peak'][2] >= 0.9)}, "
        f">= 0.8: {sum(1 for c in left if c['peak'][2] >= 0.8)}, >= 0.6: {sum(1 for c in left if c['peak'][2] >= 0.6)}")
    out(f"table {table}")
    return rows, pool, chosen


# ---------------------------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["score", "report", "selfcheck", "candidates"])
    ap.add_argument("--dataset-root", default=C.DEFAULT_DATASET_ROOT)
    ap.add_argument("--set", default="negatives-v5era", help="directory of the copied pieces and its manifest")
    ap.add_argument("--out", default=DEFAULT_OUT, help="scores directory under the dataset root")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--chunk-s", type=float, default=1200.0)
    ap.add_argument("--warm-s", type=float, default=60.0)
    ap.add_argument("--bench-s", type=float, help="(score) only time this many seconds of audio per worker; saves nothing")
    ap.add_argument("--floor", type=float, default=0.5, help="(candidates) v4 or v5 peak at least this")
    ap.add_argument("--limit", type=int, default=MAX_CANDIDATES)
    ap.add_argument("--rewrite", action="store_true", help="(candidates) replace an existing table (only when nothing is labelled)")
    ap.add_argument("--devices", help="comma list (default: every device in kivi_devices.json)")
    ap.add_argument("--devices-file")
    ap.add_argument("--context", default="grigri")
    ap.add_argument("--namespace", default="home-assistant")
    ap.add_argument("--pod", default="home-assistant-hass-0")
    ap.add_argument("--container")
    ap.add_argument("--recordings-root", default="/voice-recordings")
    ap.add_argument("--from-dir", help="local copy laid out like /voice-recordings")
    a = ap.parse_args(argv)
    {"score": cmd_score, "report": cmd_report, "selfcheck": cmd_selfcheck, "candidates": cmd_candidates}[a.what](a)


if __name__ == "__main__":
    main()
