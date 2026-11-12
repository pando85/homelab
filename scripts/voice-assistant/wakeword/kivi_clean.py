#!/usr/bin/env python3
"""Training-data cleaning before the "oye Kivi" v6.3 retry (kivi-models docs/plan/2026-11-false-wake-plan.md section 10).
Re-runs the two data-quality checks of the 2026-11-11 report (`$KIVI_HOME/quality/report.md`) on the material the retry
will train on, flags likely mislabels, auto-drops only what the agreed policy allows, and queues the rest for the owner.

    kivi_clean.py build    # inventory of the household material + features (cached per item) -> v6p3r/clean/
    kivi_clean.py oof      # grouped 5-fold out-of-fold scores, from scratch (GPU, about 15 min)
    kivi_clean.py asr      # local faster-whisper phrase flags for the items that need them (GPU; cached per item)
    kivi_clean.py report   # decisions, counts -> $KIVI_HOME/quality/report_v6p3r.md, flags CSV, drop list
    kivi_clean.py queue    # append the owner items to the labelling plan (new tier) -> `kivi_live_wakes.py queue`
    kivi_clean.py run [--queue]   # build, oof, asr, report (and queue)

Material (even Madrid days, 2026-11-12 included by the owner's decision of 2026-11-12; odd days from 2026-11-09 on are
never read; 2026-11-05 to -07 grandfathered):
  room_w8      the v6p3 builder's room negative windows (`v6p3/feat/room_train_w8.npy`, rows re-enumerated as
               `v6_room.py windows` does, for their time and TV);
  room_new     room windows of 2026-11-10 and -12 (stride 32, cached embeddings), the days the retry adds;
  mined        room moments of `labelq/moments.json` on even days where v5 >= 0.3 or v6 / v6.2 >= 0.5 (the v6p3 hard
               mining heads; the peak step +-1), unless an owner-labelled attempt is within 3 s;
  windows      labelled windows of every window table on even days (owner or trusted auto; attempt = positive);
  live         even-day live clips labelled true (positive: the last 1 s of the wake tail, or the phrase in the request
               for button / unknown triggers) or false (negative: the tail, and the request audio as conversation);
  takes + synthetic   the v6 sets the retry reuses (real session takes 2026-11-08, synthetic isolated and context
               positives, near-phrases, confuser sentences, real near-words and "Kivi" alone, background), with the
               leakage groups of the 2026-11-11 check (`quality/oof/meta_*.npz`); generic negatives stay in every fold.
Checks: (1) grouped out-of-fold scores (groups: room TV and Madrid date-hour, live clip, TTS voice, recording session;
5 folds from scratch, the 2026-11-11 recipe); thresholds as there: t_pos = mean OOF score of the real positives,
t_neg = the mean OOF score of the household negatives (confident learning: a positive is flagged when its
P(negative) reaches the negatives' mean P(negative)). (2) ASR phrase flags (local faster-whisper large-v3-turbo,
unprompted and with the hotword; transcripts stay in memory, only flags are kept) for every mined moment without earlier
ASR evidence, every live clip without autolabel evidence and every OOF-flagged item without evidence; earlier evidence
is reused (quality/hardneg_phrase_flags.csv, roomneg_phrase_flags.csv, the labelq autolabel evidence, the live autolabel
evidence, and owner labels, which stand).
Decisions: auto-drop = a negative with a trusted ASR flag (phrase or kivi word; hotword-only at word probability >= 0.5,
any for room_w8 events) as in plan section 9; everything else that is flagged goes to the owner (a new plan tier), unless
the owner already labelled it (the owner label stands, counted) or it already waits in the plan (counted). Duplicates
(identical audio, the same moment in several tables) and speaker-label conflicts are counted; conflicting labels of one
moment go to the owner.
Privacy: counts, times, scores and flags only. No audio is played or printed, no text leaves the ASR process.
"""
import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

import kivi_live_wakes as L
import kivi_real_common as C

TZ = ZoneInfo("Europe/Madrid")
KH = C.kivi_home()
ROOT = KH / "datasets" / "real"
OUT = KH / "v6p3r" / "clean"
QUAL = KH / "quality"
RATE, CHUNK, WIN = 16000, 1280, 1760
FIRST_STEP = 24
GRANDFATHER = "2026-11-08"
NEW_DAYS = ("2026-11-10", "2026-11-12")
MATCH_S = 3.0
MERGE_S = 2.0
K = 5
SEED = 20261112
STEPS, LR = 8000, 5e-4
TABLE = "clean_v6p3r.csv"
CLIPS = "clean"
FEW = 60                     # at most this many owner items (6 minutes): tier P0, first; else tier PC, after the c3-b
#                              items of P3 (owner decision 2026-11-12)
SO = ("emb_s_melrow.onnx", "src/openWakeWord/openwakeword/resources/models/embedding_model.onnx")
MINE_FLOORS = {"v5": 0.3, "v6": 0.5, "v6.2": 0.5}
W8_INDEX, W8_PARTS = "v6/room_emb/index_v6p2.json", "old-even,old-odd,even-trn"
NEW_INDEXES = ("v6/room_emb/index_v6p2.json", "labelq/room_emb/index.json")
OWNER = ("owner", "owner-button")
NEG_WINDOW = ("other-speech", "noise", "tv-media")
# v6 sets reused by the retry: name -> (path, label, per step, weight); groups from quality/oof/meta_<name>.npz
V6_SETS = {
    "pos_train": ("v6/feat/pos_train.npy", 1, 192, 1.0),
    "ctxp_syn_pos": ("v6/feat/ctxp_syn_pos.npy", 1, 64, 1.0),
    "real_tv_train": ("v6/real/real_tv_train.npy", 1, 64, 1.0),
    "real_pc_train": ("v6/real/real_pc_train.npy", 1, 48, 1.0),
    "real_webcam_train": ("v6/real/real_webcam_train.npy", 1, 16, 1.0),
    "near_train": ("v6/feat/near_train.npy", 0, 128, 3.0),
    "ctxn_syn_conf": ("v6/feat/ctxn_syn_conf.npy", 0, 64, 6.0),
    "ctxp_syn_negpart": ("v6/feat/ctxp_syn_negpart.npy", 0, 32, 3.0),
    "neg_kivi": ("v6/real/neg_kivi.npy", 0, 64, 6.0),
    "neg_near": ("v6/real/neg_near.npy", 0, 32, 6.0),
    "neg_bg": ("v6/real/neg_bg.npy", 0, 32, 3.0),
}
FIXED = (("data/acav/acav_sub400k.npy", 512, 3.0), ("v2/feat/T_mls_train0_w8.npy", 128, 3.0),
         ("v2/feat/hard_r1.npy", 128, 3.0), ("v4/feat/mls_hard4.npy", 128, 3.0))
# household sets: name -> (label, per step, weight)
HOUSE_SETS = {"room_w8": (0, 128, 3.0), "room_new": (0, 64, 3.0), "mined": (0, 64, 6.0), "win_neg": (0, 32, 6.0),
              "live_hard": (0, 32, 6.0), "live_conv": (0, 32, 6.0), "win_att": (1, 8, 1.0), "live_pos": (1, 16, 1.0)}
REAL_POS = ("real_tv_train", "real_pc_train", "real_webcam_train", "win_att", "live_pos")


# ---------------------------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------------------------


def madrid(t):
    lt = datetime.fromtimestamp(t, TZ)
    return lt.date().isoformat(), 18 <= lt.hour < 24


def is_train_day(day):
    return day < GRANDFATHER or int(day[-2:]) % 2 == 0


def room_group(device, t):
    return f"room:{device}:{datetime.fromtimestamp(t, TZ).strftime('%Y-%m-%dT%H')}"


def stamp_epoch(s):
    return datetime.strptime(s, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


def step_time(c):
    return (CHUNK * np.asarray(c) + WIN) / RATE


def step_of(sec):
    """The step whose audio ends nearest to `sec` seconds from the stream start."""
    return int(round((sec * RATE - WIN) / CHUNK))


def fold_of(group, pos_rank=None, k=K):
    """Fold of a leakage group: positive-bearing groups round-robin by their rank (so every fold keeps real positives),
    the others by a stable hash."""
    if pos_rank is not None:
        return pos_rank % k
    return int(hashlib.sha1(group.encode()).hexdigest(), 16) % k


def assign_folds(groups, pos_groups, k=K):
    ranked = sorted(pos_groups, key=lambda g: hashlib.sha1(g.encode()).hexdigest())
    rank = {g: i for i, g in enumerate(ranked)}
    return {g: fold_of(g, rank.get(g), k) for g in groups}


def merge_events(rows, merge_s=MERGE_S):
    """rows: [(device, t, score)] -> events [{device, t, t_last, score, n}] (chained within merge_s per TV; the time of
    the highest score)."""
    out = []
    for dev, t, s in sorted(rows):
        e = out[-1] if out else None
        if e and e["device"] == dev and t - e["t_last"] <= merge_s:
            e["t_last"], e["n"] = t, e["n"] + 1
            if s > e["score"]:
                e["score"], e["t"] = s, t
        else:
            out.append({"device": dev, "t": t, "t_last": t, "score": s, "n": 1})
    return out


def evidence_from_autolabel(ev):
    """ASR flag from kivi_autolabel / kivi_label_queue evidence (`ph=1;kivi=0;oye=1;weak=0;...`): phrase, kivi_word,
    weak (oye-like or a weak match), none; None when the evidence has no word fields."""
    f = dict(x.split("=", 1) for x in (ev or "").split(";") if "=" in x)
    if "ph" not in f:
        return None
    if f.get("ph") == "1":
        return "phrase"
    if f.get("kivi") == "1":
        return "kivi_word"
    if f.get("weak") == "1" or f.get("oye") == "1":
        return "weak"
    return "none"


def asr_flag(u, h, near_s=1.5):
    """Flag of the 2026-11-11 check from the unprompted (u) and hotword (h) analyses ({kind, end_off_s, word_prob};
    end_off_s None = anywhere): phrase / kivi_word (unprompted, near), hotword_only (hotword run only), none."""
    def near(r):
        return r["kind"] != "none" and (r["end_off_s"] is None or abs(r["end_off_s"]) <= near_s)
    if near(u):
        return ("phrase" if u["kind"] == "strict" else "kivi_word"), u["word_prob"]
    if near(h) and h["kind"] == "strict":
        return "hotword_only", h["word_prob"]
    return "none", None


def drop_by_asr(flag, conf, set_name):
    """Plan section 9 rule for negatives: phrase or kivi word always; hotword-only for room_w8 events at any word
    probability, else at >= 0.5."""
    if flag in ("phrase", "kivi_word"):
        return True
    if flag == "hotword_only":
        return set_name == "room_w8" or (conf or 0.0) >= 0.5
    return False


def decide(item, t_pos, t_neg):
    """Decision for one scored item: (decision, reason). Decisions: auto-drop, owner (to the owner's queue),
    owner-label-stands, already-queued, ok. Owner labels are never changed; a pending plan item is not queued twice."""
    neg = item["cls"] == 0
    oof = item.get("oof")
    flag = item.get("asr") or "none"
    if neg and drop_by_asr(flag, item.get("asr_conf"), item["set"]):
        return "auto-drop", f"asr {flag}"
    oof_flag = oof is not None and ((neg and oof >= t_pos) or (not neg and oof <= t_neg))
    asr_doubt = (neg and flag in ("hotword_only", "weak") and oof is not None and oof >= 0.5) or \
        (not neg and item["set"] == "live_pos" and flag == "none" and oof is not None and oof <= t_neg)
    if not (oof_flag or asr_doubt):
        return "ok", ""
    why = ("oof high" if neg else "oof low") if oof_flag else f"asr {flag}"
    if item.get("source") in OWNER:
        return "owner-label-stands", why
    if item.get("pending"):
        return "already-queued", why
    return "owner", why


# ---------------------------------------------------------------------------------------------
# reading (fresh, read only)
# ---------------------------------------------------------------------------------------------


def window_rows(root=ROOT):
    """Every window table row with time, source (tables without a label_source column are owner-labelled), evidence."""
    out = []
    for p in L.window_tables(root):
        for r in L.read_miss_table(p):
            try:
                t = stamp_epoch(r["utc"])
            except ValueError:
                continue
            src = r.get("label_source", "") if "label_source" in r else ("owner" if r["label"] else "")
            out.append(dict(r, t=t, table=p.name, source=src))
    return out


def plan_pending_refs(root=ROOT):
    """{(kind, table, ref)} of plan items that still wait for the owner, and the window rows they stand for."""
    items, _ = L.plan_queue_items(root, lambda r: True, set())
    return {("live" if it["kind"] == "live" else "window", it["table"].name, it["row"].get("run_id") or it["row"]["id"])
            for it in items}


def load_pieces():
    """Room pieces with cached embeddings: {clip: piece} (start epoch, steps, emb path, device, part)."""
    out = {}
    for ix in ("v6/room_emb/index.json", "labelq/room_emb/index.json", W8_INDEX):
        p = KH / ix
        if not p.exists():
            continue
        for pc in json.loads(p.read_text())["pieces"]:
            if pc["clip"] in out:
                continue
            out[pc["clip"]] = dict(pc, t0=datetime.fromisoformat(pc["start_utc"].replace("Z", "+00:00")).timestamp(),
                                   ix=ix)
    return out


def emb_path(pc):
    p = Path(pc["emb"])
    return p if p.is_absolute() else KH / p


def piece_for(pieces, device, t):
    best = None
    for pc in pieces.values():
        if pc["device"] != device:
            continue
        rel = t - pc["t0"]
        c = step_of(rel)
        if FIRST_STEP + 1 <= c < pc["steps"] - 1:
            room = min(c, pc["steps"] - c)
            if best is None or room > best[0]:
                best = (room, pc, c)
    return (best[1], best[2]) if best else (None, None)


def exclusions(windows):
    """Intervals {device: [(a, b)]} the retry excludes from room negatives: the v6p3 exclusions (ASR flags, owner
    attempts, amendment-5 events) plus every owner-labelled attempt read now."""
    iv = defaultdict(list)
    p = KH / "v6p3" / "room_exclude.json"
    if p.exists():
        for dev, xs in json.loads(p.read_text()).items():
            iv[dev] += [tuple(x) for x in xs]
    for w in windows:
        if w["label"] == "attempt":
            iv[w["device"]].append((w["t"] - MATCH_S, w["t"] + MATCH_S))
    return iv


def inside(iv, dev, t):
    return any(a <= t <= b for a, b in iv.get(dev, ()))


# ---------------------------------------------------------------------------------------------
# build: inventory + features
# ---------------------------------------------------------------------------------------------


class Embedder:
    def __init__(self):
        from fast_eval import StreamFeatures
        self.F = StreamFeatures(*(str(KH / x) for x in SO))

    def windows(self, x):
        """All (16, 96) windows of an int16 stream, as float16, and the end time (s) of each."""
        import v6_room as R
        e = R.embed_stream(self.F, x)
        if len(e) < 16:
            return np.zeros((0, 16, 96), np.float16), np.zeros(0)
        v = R.window_view(e)
        ends = step_time(np.arange(len(v)) + 15)
        ok = np.arange(len(v)) + 15 >= FIRST_STEP
        return np.asarray(v[ok], np.float16), ends[ok]


def read_wav(path):
    import soundfile as sf
    x, sr = sf.read(path, dtype="int16", always_2d=True)
    assert sr == RATE, path
    return x[:, 0]


def feat_cache(key):
    return OUT / "feat" / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".npz")


def cached_windows(emb, key, path_fn):
    p = feat_cache(key)
    if p.exists():
        z = np.load(p)
        return z["X"], z["ends"]
    X, ends = emb.windows(path_fn())
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    np.savez(p, X=X, ends=ends)
    os.chmod(p, 0o600)
    return X, ends


def w8_meta():
    """(device, t) per row of v6p3/feat/room_train_w8.npy, re-enumerated as v6_room.py windows (stride 8)."""
    import v6_room as R
    pieces = R.load_index(argparse.Namespace(index=str(KH / W8_INDEX), part=W8_PARTS, local_date=None))
    excl = json.loads((KH / "v6p3" / "room_exclude.json").read_text())
    devs, ts = [], []
    for pc in pieces:
        if pc["steps"] <= FIRST_STEP:
            continue
        idx = np.arange(FIRST_STEP - 15, pc["steps"] - 15, 8)
        t = R._utc(pc["start_utc"]).timestamp() + step_time(idx + 15)
        keep = np.ones(len(idx), bool)
        for a, b in excl.get(pc["device"], []):
            keep &= ~((t >= a) & (t <= b))
        ts.append(t[keep])
        devs += [pc["device"]] * int(keep.sum())
    return np.array(devs), np.concatenate(ts)


def cmd_build(a):
    """Inventory (items.jsonl) and household feature arrays (house_<set>.npy + rows in items)."""
    OUT.mkdir(parents=True, exist_ok=True)
    os.chmod(OUT, 0o700)
    windows = window_rows()
    pend = plan_pending_refs()
    pending_t = defaultdict(list)
    for w in windows:
        if ("window", w["table"], w["id"]) in pend or (not w["label"] and w["table"] != TABLE):
            pending_t[w["device"]].append(w["t"])
    iv = exclusions(windows)
    pieces = load_pieces()
    emb = None
    items, feats = [], defaultdict(list)
    nrows = Counter()

    def add(item, X, score_mask=None, train_stride=1):
        nonlocal nrows
        s = item["set"]
        sm = np.ones(len(X), bool) if score_mask is None else score_mask
        if train_stride > 1:                         # thin the training rows; the scoring rows always stay
            keep = sm.copy()
            keep[::train_stride] = True
            X, sm = X[keep], sm[keep]
        item["rows"] = [int(nrows[s]), int(nrows[s] + len(X))]
        item["score_rows"] = np.flatnonzero(sm).tolist()
        feats[s].append(np.asarray(X, np.float16))
        nrows[s] += len(X)
        items.append(item)

    # mined room moments (cached embeddings)
    moments = json.loads((KH / "labelq" / "moments.json").read_text())["moments"]
    hn = {}
    for name in ("hardneg_phrase_flags.csv", "roomneg_phrase_flags.csv"):
        p = QUAL / name
        if p.exists():
            for r in csv.DictReader(open(p, encoding="utf-8")):
                dev = r["device"].replace("tv-c3", "c3-")
                hn.setdefault(dev, []).append((stamp_epoch(r["event_id"].split("-", 3)[3]), r["flag"],
                                               float(r.get("confidence") or 0)))
    lq_ev = defaultdict(list)
    for w in windows:
        f = evidence_from_autolabel(w.get("auto_evidence", ""))
        if f is not None:
            lq_ev[w["device"]].append((w["t"], f, None))
        elif w["source"] in OWNER:
            lq_ev[w["device"]].append((w["t"], "owner:" + w["label"], None))
    skipped = Counter()
    for m in moments:
        if not is_train_day(m["day"]):
            continue
        sc = m["scores"]
        if not any(sc.get(h, 0.0) >= f for h, f in MINE_FLOORS.items()):
            continue
        dev, t = m["device"], m["t"]
        if inside(iv, dev, t) and any(w["label"] == "attempt" and abs(w["t"] - t) <= MATCH_S
                                      for w in windows if w["device"] == dev):
            skipped["mined: owner attempt within 3 s"] += 1
            continue
        pc, c = piece_for(pieces, dev, t)
        if pc is None:
            skipped["mined: no cached piece"] += 1
            continue
        e = np.load(emb_path(pc), mmap_mode="r")
        X = np.stack([np.asarray(e[cc - 15:cc + 1], np.float16) for cc in (c - 1, c, c + 1)])
        ev = [x for x in hn.get(dev, ()) + lq_ev.get(dev, ()) if abs(x[0] - t) <= MATCH_S]
        prior = None
        for x in ev:
            if x[1].startswith("owner:"):
                prior = prior or x
            elif prior is None or not prior[1].startswith("owner:"):
                rank = ("phrase", "kivi_word", "hotword_only", "weak", "none")
                if prior is None or rank.index(x[1]) < rank.index(prior[1]):
                    prior = x
        day, evening = madrid(t)
        add({"id": f"mined:{dev}:{L.dir_stamp(t)}", "set": "mined", "cls": 0, "device": dev, "t": t, "day": day,
             "evening": evening, "label": "", "source": "", "group": room_group(dev, t),
             "scores": sc, "pending": any(abs(x - t) <= MATCH_S for x in pending_t[dev]),
             "prior": (prior[1], prior[2]) if prior else None, "excluded_v6p3": inside(iv, dev, t)}, X)
    # labelled windows of even days
    for w in windows:
        day, evening = madrid(w["t"])
        if not is_train_day(day) or w["table"] == TABLE:
            continue
        lab = w["label"]
        definite = w["source"] in OWNER or (w["source"].startswith("auto") and lab in NEG_WINDOW)
        if not lab or lab == "unclear" or not definite:
            continue
        path = ROOT / "live-misses" / w["clip"]
        if not path.exists():
            skipped["window: clip missing"] += 1
            continue
        emb = emb or Embedder()
        X, ends = cached_windows(emb, f"win:{w['table']}:{w['id']}:{w['clip']}", lambda p=path: read_wav(p))
        sm = (ends >= 2.0) & (ends <= 4.5)
        prior = evidence_from_autolabel(w.get("auto_evidence", ""))
        add({"id": f"win:{w['table']}:{w['id']}", "set": "win_att" if lab == "attempt" else "win_neg",
             "cls": int(lab == "attempt"), "device": w["device"], "t": w["t"], "day": day, "evening": evening,
             "label": lab, "source": w["source"], "speaker": w["speaker"], "group": room_group(w["device"], w["t"]),
             "table": w["table"], "ref": w["id"], "pending": False,
             "prior": (prior, None) if prior else ("owner:" + lab, None) if w["source"] in OWNER else None,
             "audio": hashlib.sha256(path.read_bytes()[44:]).hexdigest()}, X, sm, train_stride=2)
    # live clips of even days
    for r in L.read_table(ROOT / "live" / "live_wakes.csv"):
        t = L.detection_epoch(r)
        day, evening = madrid(t)
        if not is_train_day(day) or r["label"] not in ("true", "false"):
            continue
        import kivi_autolabel as A
        src = r["label_source"]
        if src.startswith("auto") and A.is_uncertain(r):
            continue
        d = ROOT / "live" / r["device"] / r["utc"]
        tail, stt = d / "wake_tail.wav", d / "stt.wav"
        if not tail.exists():
            skipped["live: clip missing"] += 1
            continue
        emb = emb or Embedder()
        prior = evidence_from_autolabel(r["auto_evidence"])
        base = {"device": r["device"], "t": t, "day": day, "evening": evening, "label": r["label"], "source": src,
                "speaker": r["speaker"], "group": f"live:{r['run_id']}", "ref": r["run_id"], "trigger": r["trigger"],
                "pending": ("live", "live_wakes.csv", r["run_id"]) in pend, "prior": (prior, None) if prior else None,
                "audio": r["sha256"]}
        Xt, et = cached_windows(emb, f"tail:{r['run_id']}:{r['sha256']}", lambda p=tail: read_wav(p))
        if r["label"] == "true":
            if r["trigger"] == "wake":
                L_tail = float(len(read_wav(tail))) / RATE
                add(dict(base, id=f"live:{r['run_id']}", set="live_pos", cls=1), Xt, et >= L_tail - 1.0)
            elif stt.exists():
                Xs, es = cached_windows(emb, f"stt:{r['run_id']}:{r['sha256_stt']}", lambda p=stt: read_wav(p))
                pe = float(r["stt_phrase_end_s"] or 0) or None
                if pe is None:
                    skipped["live: true, button / unknown trigger, no phrase time"] += 1
                    continue
                add(dict(base, id=f"live:{r['run_id']}", set="live_pos", cls=1), Xs, np.abs(es - pe) <= 0.6)
        else:
            if r["trigger"] == "wake":
                add(dict(base, id=f"live:{r['run_id']}", set="live_hard", cls=0), Xt, train_stride=2)
            if stt.exists():
                Xs, es = cached_windows(emb, f"stt:{r['run_id']}:{r['sha256_stt']}", lambda p=stt: read_wav(p))
                if len(Xs):
                    add(dict(base, id=f"conv:{r['run_id']}", set="live_conv", cls=0,
                             prior=None if r["phrase_in_stt"] == "" else
                             (("phrase" if r["phrase_in_stt"] == "true" else "none"), None)), Xs, train_stride=4)
    # room windows of the new even days (stride 32), as negatives
    import v6_room as R
    nd = Counter()
    for pc in pieces.values():
        day = R.local_date(pc["start_utc"])
        if day not in NEW_DAYS or pc["steps"] <= FIRST_STEP:
            continue
        e = np.load(emb_path(pc), mmap_mode="r")
        v = R.window_view(e)
        idx = np.arange(FIRST_STEP - 15, len(v), 32)
        t = pc["t0"] + step_time(idx + 15)
        keep = np.array([not inside(iv, pc["device"], x) for x in t], bool)
        idx, t = idx[keep], t[keep]
        X = np.asarray(v[idx], np.float16)
        feats["room_new"].append(X)
        for tt in t:
            nd[(pc["device"], room_group(pc["device"], tt))] += 1
        items.append({"id": f"room_new:{pc['clip']}", "set": "room_new", "cls": 0, "device": pc["device"],
                      "t": float(t[0]) if len(t) else pc["t0"], "day": day, "group": "",
                      "rows": [int(nrows["room_new"]), int(nrows["room_new"] + len(X))],
                      "row_t": [round(float(x), 3) for x in t], "row_groups": [room_group(pc["device"], x) for x in t]})
        nrows["room_new"] += len(X)
    fd = OUT / "feat"
    fd.mkdir(parents=True, exist_ok=True)
    for s, parts in feats.items():
        X = np.concatenate(parts) if parts else np.zeros((0, 16, 96), np.float16)
        np.save(fd / f"house_{s}.npy", X)
        os.chmod(fd / f"house_{s}.npy", 0o600)
    with open(OUT / "items.jsonl", "w") as f:
        f.writelines(json.dumps(it) + "\n" for it in items)
    os.chmod(OUT / "items.jsonl", 0o600)
    summ = {"items": dict(Counter(it["set"] for it in items)), "rows": dict(nrows), "skipped": dict(skipped),
            "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (OUT / "build.json").write_text(json.dumps(summ, indent=1))
    print(json.dumps(summ, indent=1))


def read_items():
    with open(OUT / "items.jsonl") as f:
        return [json.loads(x) for x in f]


# ---------------------------------------------------------------------------------------------
# oof
# ---------------------------------------------------------------------------------------------


def sources():
    """[(name, X memmap, row groups, label, per step, weight)] of every scored set."""
    items = read_items()
    out = []
    for name, (path, lab, n, wt) in V6_SETS.items():
        X = np.load(KH / path, mmap_mode="r")
        g = np.load(QUAL / "oof" / f"meta_{name}.npz")["group"]
        assert len(g) == len(X), name
        out.append((name, X, g, lab, n, wt))
    X = np.load(KH / "v6p3" / "feat" / "room_train_w8.npy", mmap_mode="r")
    devs, ts = w8_meta()
    assert len(devs) == len(X), (len(devs), len(X))
    out.append(("room_w8", X, np.array([room_group(d, t) for d, t in zip(devs, ts)]), 0, *HOUSE_SETS["room_w8"][1:]))
    for s, (lab, n, wt) in HOUSE_SETS.items():
        if s == "room_w8":
            continue
        p = OUT / "feat" / f"house_{s}.npy"
        if not p.exists():
            continue
        X = np.load(p, mmap_mode="r")
        g = np.empty(len(X), dtype=object)
        for it in items:
            if it["set"] == s:
                a, b = it["rows"]
                g[a:b] = it["row_groups"] if "row_groups" in it else it["group"]
        out.append((s, X, g.astype(str), lab, n, wt))
    return out


def cmd_oof(a):
    import torch
    from openwakeword.train import Model
    from torch import nn
    torch.set_num_threads(min(8, a.threads))
    srcs = sources()
    allg = {g for _, _, gs, *_ in srcs for g in set(gs.tolist())}
    posg = {g for _, _, gs, lab, *_ in srcs if lab == 1 for g in set(gs.tolist())}
    folds = assign_folds(allg, posg)
    (OUT / "oof").mkdir(parents=True, exist_ok=True)
    os.chmod(OUT / "oof", 0o700)
    fixed = [(np.load(KH / p, mmap_mode="r"), n, wt) for p, n, wt in FIXED if (KH / p).exists()]
    scores = {s: np.full(len(X), np.nan, np.float32) for s, X, *_ in srcs}
    fold_ids = {s: np.array([folds[g] for g in gs]) for s, _, gs, *_ in srcs}
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    for k in range(K):
        t0 = time.time()
        torch.manual_seed(SEED + k)
        rng = np.random.default_rng(SEED + k)
        data = []
        for s, X, gs, lab, n, wt in srcs:
            tr = np.flatnonzero(fold_ids[s] != k)
            if len(tr):
                data.append((X, tr, min(n, len(tr)), wt, lab))
        for X, n, wt in fixed:
            data.append((X, None, n, wt, 0))
        oww = Model(n_classes=1, input_shape=(16, 96), model_type="dnn", layer_dim=64)
        net = oww.model.to(dev)
        def logits(x, net=net):
            return net.last_layer(net.blocks[0](net.relu1(net.layernorm1(net.layer1(net.flatten(x))))))
        opt = torch.optim.Adam(net.parameters(), LR)
        sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=LR, total_steps=a.steps)
        y = torch.cat([torch.full((n,), float(lab), device=dev) for _, _, n, _, lab in data])
        w = torch.cat([torch.full((n,), wt, device=dev) for _, _, n, wt, _ in data])

        def batch(X, tr, n, rng=rng):
            i = np.sort(rng.choice(len(X), n, replace=False) if tr is None else rng.choice(tr, n, replace=False))
            return torch.tensor(np.asarray(X[i], dtype=np.float32), device=dev)
        for step in range(a.steps):
            x = torch.cat([batch(X, tr, n) for X, tr, n, _, _ in data])
            loss = (nn.functional.binary_cross_entropy_with_logits(logits(x).squeeze(1), y, reduction="none") * w).mean()
            opt.zero_grad()
            loss.backward()
            opt.step()
            sched.step()
        net.eval()
        with torch.no_grad():
            for s, X, *_ in srcs:
                ho = np.flatnonzero(fold_ids[s] == k)
                for j in range(0, len(ho), 32768):
                    sl = ho[j:j + 32768]
                    xb = torch.tensor(np.asarray(X[sl], dtype=np.float32), device=dev)
                    scores[s][sl] = net(xb).reshape(-1).float().cpu().numpy()
        print(json.dumps({"fold": k, "loss": round(float(loss), 4), "s": round(time.time() - t0)}), flush=True)
    for s, v in scores.items():
        np.save(OUT / "oof" / f"oof_{s}.npy", v)
        os.chmod(OUT / "oof" / f"oof_{s}.npy", 0o600)
    (OUT / "oof" / "folds.json").write_text(json.dumps({"k": K, "seed": SEED, "steps": a.steps, "lr": LR,
                                                        "groups": len(folds), "positive_groups": len(posg)}))


# ---------------------------------------------------------------------------------------------
# asr
# ---------------------------------------------------------------------------------------------


def norm_word(t):
    import re
    import unicodedata
    t = unicodedata.normalize("NFKD", t.lower()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]+", "", t)


def analyse_words(words, centre):
    """words [(norm, start, end, prob)] -> {kind strict / kivi / none, end_off_s, word_prob} (the 2026-11-11 matcher)."""
    import re
    oye = r"(?:oye|oie|hoye|olle|oy|oi|hoy)"
    kivi = r"(?:kivi|quivi|kibi|quibi|kiwi|quiwi|kivy|kiby|kibbi|kiwy|kevi|keevee|kiwis|kivis)"
    kivi_re, oye_re = re.compile(r"^" + kivi + r"$"), re.compile(r"^" + oye + r"$")
    fused = re.compile(r"^(?:oi|oye|hoy)(?:kivi|kibi|kiwi|quivi|quibi)$")
    best = None
    for i, (t, _a, b, p) in enumerate(words):
        kind = "strict" if fused.match(t) else None
        if kind is None and kivi_re.match(t):
            kind = "strict" if (i > 0 and oye_re.match(words[i - 1][0])) else "kivi"
        if kind is None:
            continue
        off = None if centre is None else round(b - centre, 2)
        cand = (kind, off, round(float(p), 3))
        if best is None or (kind == "strict" and best[0] != "strict") or \
                (kind == best[0] and off is not None and best[1] is not None and abs(off) < abs(best[1])):
            best = cand
    return {"kind": best[0] if best else "none", "end_off_s": best[1] if best else None,
            "word_prob": best[2] if best else None}


def needs_asr(it, oof_flagged):
    if it["set"] in ("room_new", "room_w8"):
        return False
    prior = it.get("prior")
    if prior and (prior[0].startswith("owner:") or prior[0] in ("phrase", "kivi_word", "hotword_only")):
        return False
    if it["set"] == "mined":
        return prior is None or it["id"] in oof_flagged
    return prior is None or (it["id"] in oof_flagged and prior[0] in ("none", "weak"))


def audio_of(it, pieces_by_dev):
    """(int16 audio, centre s or None) for an item."""
    import kivi_label_queue as Q
    if it["set"] == "mined" or it["set"].startswith("w8"):
        pc = Q.piece_at(pieces_by_dev, it["device"], it["t"])
        if pc is None:
            return None, None
        a = max(0, int((it["t"] - pc["start"] - 4.0) * RATE))
        b = min(pc["n"], int((it["t"] - pc["start"] + 3.0) * RATE))
        x = np.fromfile(ROOT / pc["clip"], dtype="<i2", count=b - a, offset=44 + 2 * a)
        return x, it["t"] - pc["start"] - a / RATE
    if it["set"] in ("win_neg", "win_att"):
        clip = next((r["clip"] for r in L.read_miss_table(ROOT / "live-misses" / it["table"]) if r["id"] == it["ref"]), None)
        return (read_wav(ROOT / "live-misses" / clip), 3.0) if clip else (None, None)
    rows = {r["run_id"]: r for r in L.read_table(ROOT / "live" / "live_wakes.csv")}
    r = rows.get(it["ref"])
    if r is None:
        return None, None
    d = ROOT / "live" / r["device"] / r["utc"]
    if it["set"] == "live_conv":
        return (read_wav(d / "stt.wav"), None) if (d / "stt.wav").exists() else (None, None)
    tail = read_wav(d / "wake_tail.wav")
    stt = read_wav(d / "stt.wav") if (d / "stt.wav").exists() else np.zeros(0, np.int16)
    return np.concatenate([tail, stt[:3 * RATE]]), len(tail) / RATE


def cmd_asr(a):
    import glob

    import kivi_label_queue as Q
    items = read_items()
    cache_p = OUT / "asr.jsonl"
    cache = {}
    if cache_p.exists():
        for line in open(cache_p):
            r = json.loads(line)
            cache[r["id"]] = r
    flagged = set(json.loads((OUT / "oof_flagged.json").read_text())) if (OUT / "oof_flagged.json").exists() else set()
    todo = [it for it in items if needs_asr(it, flagged) and it["id"] not in cache]
    todo += w8_events_for_asr(flagged, cache)
    print(f"asr: {len(todo)} item(s) to transcribe ({len(cache)} cached)", flush=True)
    if not todo:
        return
    import torch  # noqa: F401  loads the CUDA libraries (cuBLAS) CTranslate2 needs, as quality/tools/hn_asr.py did
    from faster_whisper import WhisperModel
    snap = glob.glob(os.path.expanduser(
        "~/.cache/huggingface/hub/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/snapshots/*"))[0]
    m = WhisperModel(snap, device="cuda", compute_type="float16", cpu_threads=min(8, a.threads))
    pieces = list(Q.pieces_from_manifests(ROOT).values())
    with open(cache_p, "a") as f:
        os.chmod(cache_p, 0o600)
        for i, it in enumerate(todo, 1):
            x, centre = audio_of(it, pieces)
            if x is None or len(x) < RATE // 2:
                rec = {"id": it["id"], "flag": "no-audio", "conf": None}
            else:
                res = {}
                xf = x.astype(np.float32) / 32768.0
                for tag, kw in (("u", {}), ("h", {"hotwords": "oye Kivi"})):
                    segs, _ = m.transcribe(xf, language="es", beam_size=5, temperature=0.0,
                                           condition_on_previous_text=False, word_timestamps=True, vad_filter=False, **kw)
                    words = [(norm_word(w.word), w.start, w.end, w.probability) for s in segs for w in (s.words or [])]
                    res[tag] = analyse_words([w for w in words if w[0]], centre)
                    del words
                flag, conf = asr_flag(res["u"], res["h"])
                rec = {"id": it["id"], "flag": flag, "conf": conf}
            f.write(json.dumps(rec) + "\n")
            f.flush()
            if i % 100 == 0:
                print(f"  {i}/{len(todo)}", flush=True)


def w8_events_for_asr(flagged, cache):
    """OOF-flagged room_w8 events (from oof_flagged.json `w8:` ids) without ASR evidence, as pseudo items."""
    out = []
    for fid in sorted(flagged):
        if not fid.startswith("w8:") or fid in cache:
            continue
        _, dev, stamp = fid.split(":", 2)
        out.append({"id": fid, "set": "w8", "device": dev, "t": stamp_epoch(stamp)})
    return out


# ---------------------------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------------------------


def item_scores(items):
    """{item id: OOF score (max over its scoring rows)} of the household items; w8 events from row scores."""
    out = {}
    cache = {}
    for it in items:
        s = it["set"]
        if s == "room_new":
            continue
        if s not in cache:
            p = OUT / "oof" / f"oof_{s}.npy"
            cache[s] = np.load(p) if p.exists() else None
        v = cache[s]
        if v is None:
            continue
        a, _b = it["rows"]
        sc = v[a + np.array(it["score_rows"], int)] if it["score_rows"] else np.array([np.nan])
        out[it["id"]] = float(np.nanmax(sc)) if np.isfinite(sc).any() else None
    return out


def room_events_flagged(t_pos):
    """OOF-flagged room rows (room_w8, room_new) merged into events: [{id, set, device, t, score, n}]."""
    out = []
    p = OUT / "oof" / "oof_room_w8.npy"
    if p.exists():
        v = np.load(p)
        devs, ts = w8_meta()
        hit = np.flatnonzero(v >= t_pos)
        for e in merge_events([(devs[i], float(ts[i]), float(v[i])) for i in hit]):
            out.append(dict(e, set="room_w8", id=f"w8:{e['device']}:{L.dir_stamp(e['t'])}"))
    p = OUT / "oof" / "oof_room_new.npy"
    if p.exists():
        v = np.load(p)
        rows = []
        for it in read_items():
            if it["set"] == "room_new":
                a, _ = it["rows"]
                for j, t in enumerate(it["row_t"]):
                    if v[a + j] >= t_pos:
                        rows.append((it["device"], t, float(v[a + j])))
        for e in merge_events(rows):
            out.append(dict(e, set="room_new", id=f"w8:{e['device']}:{L.dir_stamp(e['t'])}"))
    return out


def thresholds(items, scores):
    real = []
    for s in ("real_tv_train", "real_pc_train", "real_webcam_train"):
        p = OUT / "oof" / f"oof_{s}.npy"
        if p.exists():
            real += np.load(p).tolist()
    real += [scores[it["id"]] for it in items if it["cls"] == 1 and scores.get(it["id"]) is not None]
    negs = [scores[it["id"]] for it in items if it.get("cls") == 0 and it["set"] != "room_new"
            and scores.get(it["id"]) is not None]
    t_pos = float(np.nanmean(real)) if real else 0.74
    t_neg = float(np.nanmean(negs)) if negs else 0.03       # confident learning: 1 - t_neg = mean P(neg) of the negatives
    return t_pos, min(t_neg, t_pos)


def synthetic_summary(t_pos, t_neg):
    out = {}
    for name, (_p, lab, _n, _w) in V6_SETS.items():
        p = OUT / "oof" / f"oof_{name}.npy"
        if not p.exists():
            continue
        v = np.load(p)
        v = v[np.isfinite(v)]
        out[name] = {"rows": len(v), "flagged": int((v <= t_neg).sum() if lab else (v >= t_pos).sum()),
                     "median": round(float(np.median(v)), 3) if len(v) else None}
    return out


def duplicates(items):
    """Counts of identical audio and of moments that several tables hold, with label agreement."""
    out = Counter()
    by_audio = defaultdict(list)
    for it in items:
        if it.get("audio"):
            by_audio[it["audio"]].append(it)
    conflicts = []
    for _h, xs in by_audio.items():
        if len(xs) > 1:
            out["identical audio groups"] += 1
            out["identical audio items (extra copies)"] += len(xs) - 1
            if len({x["label"] for x in xs}) > 1:
                out["identical audio with different labels"] += 1
                conflicts.append(xs)
    wins = [w for w in window_rows() if w["label"] and w["label"] != "unclear" and is_train_day(madrid(w["t"])[0])]
    wins.sort(key=lambda w: (w["device"], w["t"]))
    i = 0
    while i < len(wins):
        j = i + 1
        while j < len(wins) and wins[j]["device"] == wins[i]["device"] and wins[j]["t"] - wins[j - 1]["t"] <= MATCH_S:
            j += 1
        grp = wins[i:j]
        if len(grp) > 1:
            out["same moment in several window rows"] += 1
            att = {w["label"] == "attempt" for w in grp}
            if len(att) > 1:
                out["same moment: attempt against non-attempt"] += 1
                conflicts.append([{"id": f"win:{w['table']}:{w['id']}", "device": w["device"], "t": w["t"],
                                   "label": w["label"], "source": w["source"], "table": w["table"], "ref": w["id"]}
                                  for w in grp])
            spk = {w["speaker"] for w in grp if w["speaker"] and w["speaker"] != "unknown"}
            if len(spk) > 1:
                out["same moment: different speakers"] += 1
        i = j
    return out, conflicts


def speaker_conflicts():
    """Auto speaker (labelq key, the autolabel's ECAPA guess) against the owner's speaker on the same live clip or
    window: Counter of (auto, owner) pairs."""
    import kivi_label_queue as Q
    key = {(k["kind"], k["ref"]): k for k in Q.read_csv(ROOT / "labelq" / "key.csv", Q.KEY_FIELDS)}
    live = {r["run_id"]: r for r in L.read_table(ROOT / "live" / "live_wakes.csv")}
    win = {r["id"]: r for r in L.read_miss_table(ROOT / "live-misses" / "labelq_v6p3r.csv")}
    out = Counter()
    for (kind, ref), k in key.items():
        if not k["auto_speaker"]:
            continue
        r = live.get(ref) if kind == "live" else win.get(ref)
        if not r or r.get("label_source") not in OWNER or not r.get("speaker"):
            continue
        out[f"{kind}: auto {k['auto_speaker']} -> owner {r['speaker']}"] += 1
    return out


def cmd_report(a):
    items = read_items()
    scores = item_scores(items)
    t_pos, t_neg = thresholds(items, scores)
    asr = {}
    if (OUT / "asr.jsonl").exists():
        for line in open(OUT / "asr.jsonl"):
            r = json.loads(line)
            asr[r["id"]] = r
    pend = plan_pending_refs()
    windows = window_rows()
    pend_t = defaultdict(list)
    for w in windows:
        if ("window", w["table"], w["id"]) in pend:
            pend_t[w["device"]].append(w["t"])
    owner_t = defaultdict(list)
    for w in windows:
        if w["source"] in OWNER and w["label"]:
            owner_t[w["device"]].append(w["t"])
    rows = []
    flagged_ids = []
    for it in items:
        if it["set"] == "room_new":
            continue
        it = dict(it, oof=scores.get(it["id"]))
        pr = it.get("prior")
        if it["id"] in asr:
            it["asr"], it["asr_conf"] = asr[it["id"]]["flag"], asr[it["id"]]["conf"]
        elif pr:
            it["asr"], it["asr_conf"] = (None if pr[0].startswith("owner:") else pr[0]), pr[1]
        if it["set"] == "mined" and any(abs(x - it["t"]) <= MATCH_S for x in owner_t[it["device"]]):
            it["source"] = "owner"
        it["pending"] = it.get("pending") or (it["set"] == "mined" and any(abs(x - it["t"]) <= MATCH_S
                                                                           for x in pend_t[it["device"]]))
        dec, why = decide(it, t_pos, t_neg)
        if dec == "auto-drop" and it.get("excluded_v6p3"):
            why += " (already excluded by v6p3)"
        if it.get("oof") is not None and ((it["cls"] == 0 and it["oof"] >= t_pos) or (it["cls"] == 1 and it["oof"] <= t_neg)):
            flagged_ids.append(it["id"])
        rows.append(dict(it, decision=dec, why=why))
    for e in room_events_flagged(t_pos):
        flagged_ids.append(e["id"])
        near_owner = any(abs(x - e["t"]) <= MATCH_S for x in owner_t[e["device"]])
        near_pend = any(abs(x - e["t"]) <= MATCH_S for x in pend_t[e["device"]])
        near_mined = next((r for r in rows if r["set"] == "mined" and r["device"] == e["device"]
                           and abs(r["t"] - e["t"]) <= MATCH_S), None)
        it = {"id": e["id"], "set": e["set"], "cls": 0, "device": e["device"], "t": e["t"],
              "day": madrid(e["t"])[0], "evening": madrid(e["t"])[1], "label": "", "group": room_group(e["device"], e["t"]),
              "source": "owner" if near_owner else "", "pending": near_pend, "oof": e["score"]}
        if e["id"] in asr:
            it["asr"], it["asr_conf"] = asr[e["id"]]["flag"], asr[e["id"]]["conf"]
        elif near_mined is not None:
            it["asr"], it["asr_conf"] = near_mined.get("asr"), near_mined.get("asr_conf")
        dec, why = decide(it, t_pos, t_neg)
        if near_mined is not None and dec == "owner":
            dec, why = "same-as-mined", f"same moment as {near_mined['id']}"
        rows.append(dict(it, decision=dec, why=why))
    (OUT / "oof_flagged.json").write_text(json.dumps(sorted(set(flagged_ids))))
    dup, conflicts = duplicates(items)
    for grp in conflicts:
        for x in grp:
            if x.get("source") in OWNER:
                continue
            rows.append({"id": "conflict:" + x["id"], "set": "conflict", "cls": 0, "device": x["device"], "t": x["t"],
                         "day": madrid(x["t"])[0], "evening": madrid(x["t"])[1], "label": x["label"],
                         "source": x.get("source", ""), "table": x.get("table"), "ref": x.get("ref"),
                         "decision": "owner", "why": "label conflict on one moment", "oof": None})
    spk = speaker_conflicts()
    write_outputs(rows, t_pos, t_neg, dup, spk, scores)


def write_outputs(rows, t_pos, t_neg, dup, spk, scores):
    QUAL.mkdir(exist_ok=True)
    fields = ("id", "set", "cls", "device", "utc", "day", "evening", "label", "source", "speaker", "oof", "asr",
              "asr_conf", "decision", "why")
    flagged = [r for r in rows if r["decision"] != "ok"]
    with open(QUAL / "clean_v6p3r_flags.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in sorted(flagged, key=lambda r: (r["decision"], r["set"], r["t"])):
            w.writerow(dict(r, utc=L.dir_stamp(r["t"]), oof="" if r.get("oof") is None else f"{r['oof']:.4f}",
                            evening=int(bool(r.get("evening")))))
    os.chmod(QUAL / "clean_v6p3r_flags.csv", 0o600)
    drop = {"intervals": defaultdict(list), "live_run_ids": [], "window_ids": []}
    for r in rows:
        if r["decision"] != "auto-drop":
            continue
        if r["set"] in ("live_hard", "live_conv"):
            drop["live_run_ids"].append(r["ref"])
        elif r["set"] == "win_neg":
            drop["window_ids"].append(f"{r['table']}:{r['ref']}")
        else:
            drop["intervals"][r["device"]].append([r["t"] - 2.0, r["t"] + 2.0])
    drop["live_run_ids"] = sorted(set(drop["live_run_ids"]))
    drop["note"] = ("negatives the retry drops (plan section 9 ASR rule), on top of v6p3/room_exclude.json; "
                    "written by kivi_clean.py report")
    (OUT / "drop.json").write_text(json.dumps(drop, indent=1))
    os.chmod(OUT / "drop.json", 0o600)
    cnt = Counter((r["set"], r["decision"]) for r in rows)
    sets = sorted({r["set"] for r in rows})
    per_set = {s: {d: cnt[(s, d)] for d in ("ok", "auto-drop", "owner", "already-queued", "owner-label-stands",
                                            "same-as-mined") if cnt[(s, d)]} | {"items": sum(cnt[(s, d)] for d in
                                                                                          {k[1] for k in cnt})}
               for s in sets}
    why = Counter((r["set"], r["decision"], r["why"]) for r in flagged)
    asr_cnt = Counter((r["set"], r.get("asr") or "no evidence") for r in rows if r["set"] != "conflict")
    owner_items = [r for r in rows if r["decision"] == "owner"]
    summ = {"t_pos": round(t_pos, 4), "t_neg": round(t_neg, 4), "per_set": per_set,
            "why": {f"{s}|{d}|{w}": n for (s, d, w), n in sorted(why.items())},
            "asr": {f"{s}|{f}": n for (s, f), n in sorted(asr_cnt.items())}, "duplicates": dict(dup),
            "speaker_auto_vs_owner": dict(spk), "owner_items": len(owner_items),
            "synthetic_and_takes": synthetic_summary(t_pos, t_neg),
            "build": json.loads((OUT / "build.json").read_text()) if (OUT / "build.json").exists() else {},
            "reported_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (OUT / "summary.json").write_text(json.dumps(summ, indent=1))
    os.chmod(OUT / "summary.json", 0o600)
    (QUAL / "report_v6p3r.md").write_text(render(summ))
    os.chmod(QUAL / "report_v6p3r.md", 0o600)
    print(json.dumps({k: summ[k] for k in ("t_pos", "t_neg", "per_set", "owner_items", "duplicates",
                                           "speaker_auto_vs_owner")}, indent=1))


SET_NAMES = {"room_w8": "room negative windows of the v6p3 builder (OOF events)", "room_new": "room windows 11-10 / 11-12 (OOF events)",
             "mined": "mined room moments (hard negatives)", "win_neg": "labelled windows, non-attempt (negatives)",
             "win_att": "owner-labelled attempt windows (positives)", "live_hard": "live false-wake clips (tail)",
             "live_conv": "request audio after false wakes (conversation)", "live_pos": "live calls (positives)",
             "conflict": "label conflicts on one moment"}


def render(s):
    intro = (f"Generated by `kivi_clean.py report` at {s['reported_utc']} from the labels as they stood (read fresh). "
             "No audio was played, printed or saved by the checks; ASR transcripts stayed in memory (local "
             "faster-whisper large-v3-turbo); only flags were kept. Day rule: even Madrid days (2026-11-12 included, "
             "owner decision 2026-11-12) and the grandfathered days before 2026-11-08; no odd day from 2026-11-09 on "
             "was read.")
    material = ("- **Material.** The v6p3 builder's room negative windows (re-enumerated for time and TV), room windows "
                "of 2026-11-10 and -12 (stride 32), the mined room moments of the v6p3 heads on even days (v5 >= 0.3, "
                "v6 / v6.2 >= 0.5, the peak step +-1), every labelled window of an even day (owner or trusted auto), "
                "every even-day live clip with a trusted label (calls: the last 1 s of the wake tail; false wakes: the "
                "tail and the request audio), and the v6 sets the retry reuses (real takes, synthetic, real "
                "near-words, background).")
    oof = ("- **OOF.** 5 grouped folds from scratch (groups: TV and Madrid date-hour for room audio, live clip, TTS "
           "voice, recording session; positive-bearing groups spread round-robin), 8,000 steps, lr 5e-4, layer 64, "
           f"generic negatives in every fold. Thresholds: t_pos = {s['t_pos']} (mean OOF of the real positives), "
           f"t_neg = {s['t_neg']} (mean OOF of the household negatives). Negative flagged: OOF >= t_pos; "
           "positive flagged: OOF <= t_neg. Item score = max over its rows; room rows merged into events within 2 s.")
    asr = ("- **ASR.** Unprompted and hotword runs; flags phrase / kivi word / hotword-only / none as in the "
           "2026-11-11 check, plus the autolabel's evidence (weak = an oye-like or weak match) where it exists; owner "
           "labels stand and are not re-transcribed.")
    dec = ("- **Decisions.** auto-drop: a negative with a trusted ASR flag (plan section 9 rule). owner: flagged and "
           "not owner-labelled and not already waiting in the plan (goes to the owner's queue). owner-label-stands: "
           "flagged, but the owner labelled it (counted, unchanged). already-queued: flagged and already in the plan.")
    out = ["# \"oye Kivi\" training-data cleaning before the v6.3 retry (counts only)", "", intro, "", "## Method", "",
           material, oof, asr, dec, "", "## Counts per set", "",
           "| set | items | ok | auto-drop | to the owner | already queued | owner label stands | same as a mined moment |",
           "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for k, v in s["per_set"].items():
        out.append(f"| {SET_NAMES.get(k, k)} | {v['items']} | {v.get('ok', 0)} | {v.get('auto-drop', 0)} | "
                   f"{v.get('owner', 0)} | {v.get('already-queued', 0)} | {v.get('owner-label-stands', 0)} | "
                   f"{v.get('same-as-mined', 0)} |")
    why = "; ".join(f"{k.replace('|', ' / ')} {n}" for k, n in s["why"].items())
    asr_ev = "; ".join(f"{k.replace('|', ' ')} {n}" for k, n in s["asr"].items())
    dup = ", ".join(f"{k} {v}" for k, v in s["duplicates"].items()) or "none"
    spk = ", ".join(f"{k} {v}" for k, v in s["speaker_auto_vs_owner"].items()) or "none yet"
    out += ["", f"Why (flagged items): {why}.", "", f"ASR evidence per set: {asr_ev}.", "",
            "## Duplicates and speaker labels", "", f"Duplicates: {dup}.", "",
            (f"Auto speaker (the autolabel's ECAPA guess, in `labelq/key.csv`) against the owner's speaker on the same "
             f"item: {spk}."), "",
            "## Synthetic sets and real takes (OOF only; not for the owner)", "",
            "| set | rows | flagged | median OOF |", "| --- | --- | --- | --- |"]
    for k, v in s["synthetic_and_takes"].items():
        out.append(f"| {k} | {v['rows']} | {v['flagged']} | {v['median']} |")
    b = s.get("build", {})
    owner = (f"To the owner: **{s['owner_items']}** item(s) (`kivi_clean.py queue` adds them to the labelling plan as "
             f"tier P0 when at most {FEW}, else PC after the c3-b items of P3; the owner runs the same "
             "`kivi_live_wakes.py queue`).")
    files = ("Files (private): `v6p3r/clean/` (items.jsonl, feat/, oof/, asr.jsonl, summary.json, drop.json), "
             "`quality/clean_v6p3r_flags.csv` (one row per flagged item: times, scores, flags, decision).")
    out += ["", f"Build: items {b.get('items')}, feature rows {b.get('rows')}, skipped {b.get('skipped')}.", "",
            owner, "", files, "", "Re-run: `kivi_clean.py run` (then `kivi_clean.py queue`).", ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------
# queue (append only; the owner's labels are never touched)
# ---------------------------------------------------------------------------------------------


def queue_running():
    """True when a `kivi_live_wakes.py queue` process runs (one look at /proc, no polling)."""
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmd = (p / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(x.endswith(b"kivi_live_wakes.py") for x in cmd) and b"queue" in cmd:
            return True
    return False


def insert_tier(plan, add):
    """The plan with `add` (all of one tier, in their own order) placed where kivi_label_queue.plan_key puts that tier:
    P0 first; PC before the first P3 item of the c3-a day block, or before P4, or at the end. Other items keep their
    order."""
    if not add:
        return plan
    if add[0]["tier"] == "P0":
        return add + plan
    at = next((i for i, p in enumerate(plan) if p["tier"] == "P4" or p["tier"] == "PC"
               or (p["tier"] == "P3" and p["evening"] != "1" and p["device"] != "c3-b")), len(plan))
    while at < len(plan) and plan[at]["tier"] == "PC":            # after the PC items of an earlier run
        at += 1
    return plan[:at] + add + plan[at:]


def cmd_queue(a):
    import kivi_label_queue as Q
    flags = list(csv.DictReader(open(QUAL / "clean_v6p3r_flags.csv", encoding="utf-8")))
    owner = [r for r in flags if r["decision"] == "owner"]
    tier = "P0" if len(owner) <= FEW else "PC"
    order = {"conflict": 0, "live_pos": 1, "win_att": 1, "live_hard": 2, "live_conv": 2}
    owner.sort(key=lambda r: (order.get(r["set"], 3) if not r["id"].startswith("conflict:") else 0,
                              -float(r["oof"] or 0)))
    plan_path = ROOT / "labelq" / "plan.csv"
    plan = Q.read_csv(plan_path, Q.PLAN_FIELDS)
    have = {(p["kind"], p["table"], p["ref"]) for p in plan}
    table = ROOT / "live-misses" / TABLE
    cur = L.read_miss_table(table)
    cur_t = defaultdict(list)
    for r in cur:
        cur_t[r["device"]].append(stamp_epoch(r["utc"]))
    pieces = list(Q.pieces_from_manifests(ROOT).values())
    new_rows, items = [], []
    why = Counter()
    for r in owner:
        t = stamp_epoch(r["utc"])
        day, ev = madrid(t)
        split = "train" if is_train_day(day) else "test"
        sid = r["id"].replace("conflict:", "")
        if sid.startswith("live:") or sid.startswith("conv:"):
            ref = sid.split(":", 1)[1]
            key = ("live", "live_wakes.csv", ref)
            if key in have:
                why["already in the plan"] += 1
                continue
            items.append(Q._plan_item("live", ref, tier, r["device"], L.dir_stamp(t), split, str(int(ev))))
            have.add(key)
            continue
        if sid.startswith("win:"):
            _, tab, ref = sid.split(":", 2)
            key = ("window", tab, ref)
            if key in have:
                why["already in the plan"] += 1
                continue
            items.append(dict(Q._plan_item("window", ref, tier, r["device"], L.dir_stamp(t), split, str(int(ev))),
                              table=tab))
            have.add(key)
            continue
        if any(abs(x - t) <= MATCH_S for x in cur_t[r["device"]]):
            why["window already cut"] += 1
            continue
        pc = Q.piece_at(pieces, r["device"], t)
        if pc is None:
            why["no local room piece"] += 1
            continue
        pcm = Q.window_pcm(ROOT, pc, t)
        rel = f"{CLIPS}/{r['device']}/{L.dir_stamp(t)}/window.wav"
        dst = ROOT / "live-misses" / rel
        if not dst.exists():
            C.private_mkdir(dst.parent, ROOT)
            C.write_wav_new(dst, pcm)
        import kivi_replay_mine as KR
        rms, pkd = KR.wav_levels(pcm)
        row = {k: "" for k in Q.FIELDS} | {
            "id": f"cl-{len(cur) + len(new_rows) + 1:05d}", "device": r["device"], "part": f"clean-{r['set']}",
            "utc": L.dir_stamp(t), "level_dbfs": rms, "peak_dbfs": pkd, "clip": rel, "split": split, "tier": tier,
            "sources": r["set"], "evening": str(int(ev)), "peaks": f"oof={r['oof']}" if r["oof"] else ""}
        new_rows.append(row)
        cur_t[r["device"]].append(t)
        items.append(dict(Q._plan_item("window", row["id"], tier, r["device"], row["utc"], split, str(int(ev))),
                          table=TABLE))
    if a.dry_run:
        print(json.dumps({"new windows": len(new_rows), "plan items": len(items), "skipped": dict(why)}, indent=1))
        return
    if new_rows:
        if table.exists():
            Q.backup(table)
        fresh = L.read_miss_table(table)                    # fresh read right before the write
        L.write_miss_table(table, fresh + new_rows)
    if items:
        Q.backup(plan_path)
        plan = Q.read_csv(plan_path, Q.PLAN_FIELDS)         # fresh read; the queue itself never writes the plan
        have = {(p["kind"], p["table"], p["ref"]) for p in plan}
        add = [it for it in items if (it["kind"], it["table"], it["ref"]) not in have]
        plan = insert_tier(plan, add)
        for i, it in enumerate(plan, 1):
            it["order"] = str(i)
        L.write_csv_atomic(plan_path, Q.PLAN_FIELDS, plan)
    print(json.dumps({"new windows": len(new_rows), "plan items added": len(items), "skipped": dict(why),
                      "queue running now (it picks the new items up at its next start)": queue_running()}, indent=1))


# ---------------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------------


def cmd_run(a):
    me = [sys.executable, os.path.abspath(__file__)]
    for step in ("build", "oof", "report", "asr", "report"):
        print(f"== {step}", flush=True)
        subprocess.run(me + [step, "--threads", str(a.threads), "--steps", str(a.steps)], check=True)
    if a.queue:
        subprocess.run(me + ["queue"], check=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["build", "oof", "asr", "report", "queue", "run"])
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--steps", type=int, default=STEPS)
    ap.add_argument("--queue", action="store_true", help="(run) also append the owner items to the plan")
    ap.add_argument("--dry-run", action="store_true", help="(queue) counts only")
    a = ap.parse_args(argv)
    {"build": cmd_build, "oof": cmd_oof, "asr": cmd_asr, "report": cmd_report, "queue": cmd_queue, "run": cmd_run}[a.what](a)


if __name__ == "__main__":
    main()
