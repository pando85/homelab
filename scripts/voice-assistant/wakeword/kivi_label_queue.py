#!/usr/bin/env python3
"""The prioritised labelling plan of the "oye Kivi" household audio before the v6.3 retry (owner decision 2026-11-12:
"label correctly the audio and a retrain with more information"; kivi-models docs/plan/2026-11-false-wake-plan.md
section 10). Counts only: no audio is played, printed or sent anywhere; transcripts live in memory (local faster-whisper,
never the cluster Whisper) and speaker embeddings are never saved.

    kivi_label_queue.py scan     # six heads (v5, v6, v6.2, v6.3 arms p3a / p3b / p3c) on the cached room embeddings
                                 # (v6/room_emb plus labelq/room_emb) -> $KIVI_HOME/labelq/moments.json (times, scores)
    kivi_label_queue.py build    # candidates P1-P4, dedup, 6 s windows, local autolabel, the window table, plan and key
    kivi_label_queue.py report   # counts per source and day parity, owner minutes, what the labels add (fresh read)
    kivi_label_queue.py audit    # after the owner's labels: auto label against owner label on the blind 5 % sample
    kivi_label_queue.py reorder  # the plan in priority order again (only the order changes)

Sources, in the owner's order (an item that several sources find keeps the first and lists all):
  P1  the true-grid room events of the fair comparison that no labelled window covers (audit/fair_fa/unlabelled_events.csv);
  P2  live clips without an owner label (wake and button runs; `live/live_wakes.csv`, already autolabelled by the collect);
  P3  room moments where any head reaches raw >= 0.5 (2 s refractory, heads merged within 2 s), every day with cached
      embeddings; c3-b evenings first, then c3-a evenings, c3-b days, c3-a days; even days before odd days inside each;
  P4  possible missed calls: a moment at raw >= 0.3 of any head followed within 30 s on the same TV by a live run (a wake
      or a mic-button press; the detection time of a live_wakes.csv row).
Dedup: a moment within 3 s of a window row of any `live-misses/*.csv` table (labelled, or pending in the queue) or inside a
live clip's span is not added again (P1 is only matched against definite labels: its `unclear` windows come back); two
windows with identical audio (sha256) are one.

Autolabel (label_source auto-v1, the trust policy of 2026-11-12): windows are transcribed (whole 6 s and the 3 s around
the peak) and judged with kivi_autolabel's word matching: phrase -> `attempt`, oye-like / kivi-like / weak -> `unclear`,
no words and almost no speech -> `noise` (0.95), no words with speech -> `unclear`, unrelated words at a mean word
confidence >= 0.6 -> `other-speech` (0.75, speaker by ECAPA), else `unclear`. Trusted (written into the table): `noise`
and `other-speech`. To the owner (table cell left empty, the auto label only in the key): `attempt`, `unclear`, and
anything below 0.7. Live clips keep the collect's autolabel; to the owner: kivi_autolabel.is_uncertain rows and every
auto `true` (a call: confirmed and speaker-labelled by ear). A seeded random 5 % of the trusted auto labels (windows and
live auto `false`) go to the owner too, blind (the queue never shows a label; the auto label stays in the key).

Files (private, mode 600/700): `<dataset>/live-misses/labelq_v6p3r.csv` (window table; MISS_FIELDS plus label_source,
label_conf, speaker_conf, auto_evidence, split, tier, sources, evening, peaks), its clips under `live-misses/labelq/`,
`<dataset>/labelq/plan.csv` (what `kivi_live_wakes.py queue` asks first, in order) and `<dataset>/labelq/key.csv` (auto
labels and why each item goes to the owner; never read by the queue). `build` is append-only: rows and plan items that
exist are kept (owner labels win), new candidates are appended.
Day split: Europe/Madrid day of the month odd = test, even = train; days before 2026-11-08 are train (grandfathered).
"""
import argparse
import csv
import hashlib
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import kivi_live_wakes as L  # noqa: E402
import kivi_real_common as C  # noqa: E402

TZ = ZoneInfo("Europe/Madrid")
RATE = C.RATE
KH = C.kivi_home()
WORK = KH / "labelq"
HEADS = {"v5": "releases/v5-oye-kivi-2026-11-08/model/oye_kivi_next_uncalibrated.onnx",
         "v6": "releases/v6-oye-kivi-2026-11-11/model/oye_kivi_ctx_uncalibrated.onnx",
         "v6.2": "releases/v6-p2-oye-kivi-2026-11-12/model/oye_kivi_ctx2_uncalibrated.onnx",
         "p3a": "v6p3/runs/p3a/kivi.onnx", "p3b": "v6p3/runs/p3b/kivi.onnx", "p3c": "v6p3/runs/p3c/kivi.onnx"}
INDEXES = ("v6/room_emb/index.json", "labelq/room_emb/index.json")
MANIFESTS = ("negatives", "negatives-v5era", "negatives-daily")
FAIR_EVENTS = "audit/fair_fa/unlabelled_events.csv"
SCAN_FLOOR = 0.3
P3_FLOOR = 0.5
P4_FLOOR = 0.3
FOLLOW_S = 30.0
MERGE_S = 2.0
MATCH_S = 3.0
WIN_S = 6.0
MID = (1.0, 4.0)                 # the 3 s around the peak inside the window (the peak is at 3 s, the phrase ends there)
GRANDFATHER = "2026-11-08"
TABLE = "labelq_v6p3r.csv"
CLIPS = "labelq"
EXTRA = ("label_source", "label_conf", "speaker_conf", "auto_evidence", "split", "tier", "sources", "evening", "peaks")
FIELDS = L.MISS_FIELDS + EXTRA
TRUSTED_WIN = ("noise", "other-speech")
OWNER_CONF = 0.7
AUDIT_SHARE = 0.05
SEED = 20261112
SECONDS_PER_ITEM = 6.0
PLAN_FIELDS = L.PLAN_FIELDS
KEY_FIELDS = ("kind", "ref", "tier", "auto_label", "auto_conf", "auto_speaker", "audit", "why")
TIERS = ("P1", "P2", "P3", "P4")


# ---------------------------------------------------------------------------------------------
# small helpers (pure)
# ---------------------------------------------------------------------------------------------


def madrid(t):
    lt = datetime.fromtimestamp(t, TZ)
    return lt.date().isoformat(), 18 <= lt.hour < 24


def split_of(day):
    if day < GRANDFATHER:
        return "train"
    return "test" if int(day[-2:]) % 2 else "train"


def utc_epoch(stamp):
    return datetime.strptime(stamp, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


def iso_epoch(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def merge_detections(dets, merge_s=MERGE_S):
    """dets: [(t, head, score)] of one TV -> moments [{t, scores {head: max}}]; detections closer than merge_s chain into
    one moment, whose time is that of its highest score."""
    out, cur = [], None
    for t, h, s in sorted(dets):
        if cur is not None and t - cur["t_last"] <= merge_s:
            cur["t_last"] = t
            cur["scores"][h] = max(cur["scores"].get(h, 0.0), s)
            if s > cur["best"]:
                cur["best"], cur["t"] = s, t
        else:
            cur = {"t": t, "t_last": t, "best": s, "scores": {h: s}}
            out.append(cur)
    return [{"t": m["t"], "scores": {h: round(v, 4) for h, v in m["scores"].items()}} for m in out]


def p3_order(c):
    """Sort key inside P3: c3-b evenings, c3-a evenings, c3-b days, c3-a days; even (train) before odd (test) days; then
    the highest score first."""
    block = (0 if c["evening"] else 2) + (0 if c["device"] == "c3-b" else 1)
    return (block, c["split"] != "train", -max(c["scores"].values() or [0.0]), c["t"])


def decide_window(ev):
    """Window label from the combined word evidence (kivi_autolabel.match_words / combine_matches) plus speech_mid_s:
    (label, confidence). Mirrors kivi_autolabel.decide_label for a 6 s window without request audio."""
    import kivi_autolabel as A
    if ev["phrase"]:
        return "attempt", 0.92 + (0.03 if ev.get("phrase_both") else 0.0)
    if ev["weak"] or ev["oye_like"] or ev["kivi_near"]:
        return "unclear", 0.5
    if ev["n_words"] == 0:
        return ("noise", 0.95) if ev["speech_mid_s"] < A.LOW_SPEECH_S else ("unclear", 0.4)
    if ev["mean_prob"] >= A.WORD_CONF_MIN:
        return "other-speech", 0.75
    return "unclear", 0.45


def to_owner_window(label, conf):
    return label not in TRUSTED_WIN or conf < OWNER_CONF


def to_owner_live(row):
    """A live clip the owner labels: the uncertain ones of the trust policy and every auto `true` (a call)."""
    import kivi_autolabel as A
    if row["label"] and not row["label_source"].startswith("auto"):
        return None
    if A.is_uncertain(row):
        return "uncertain" if row["label"] else "unlabelled"
    if row["label"] == "true":
        return "call"
    return None


def audit_sample(refs, share=AUDIT_SHARE, seed=SEED):
    """A seeded random share (rounded up, at least 1 when there is any) of the trusted auto-labelled refs."""
    refs = sorted(refs)
    if not refs:
        return set()
    k = max(1, math.ceil(share * len(refs)))
    return set(random.Random(seed).sample(refs, k))


# ---------------------------------------------------------------------------------------------
# room pieces
# ---------------------------------------------------------------------------------------------


def pieces_from_manifests(root):
    """{(device, clip): {clip, device, start, n, part}} of every copied room piece that exists locally."""
    out = {}
    for name in MANIFESTS:
        mf = Path(root) / name / "manifest.jsonl"
        if not mf.exists():
            continue
        for r in C.read_jsonl(mf):
            if r.get("superseded"):
                continue
            p = Path(root) / r["clip"]
            if not p.exists():
                continue
            out[(r["device"], r["clip"])] = {"clip": r["clip"], "device": r["device"], "start": iso_epoch(r["start_utc"]),
                                             "n": max(0, (p.stat().st_size - 44) // 2), "part": r.get("split") or ""}
    return out


def piece_at(pieces, device, t, pad_s=WIN_S / 2):
    """The piece of `device` that holds t with the most room around it (a window must fit)."""
    best = None
    for pc in pieces:
        if pc["device"] != device:
            continue
        a, b = pc["start"], pc["start"] + pc["n"] / RATE
        if a <= t <= b:
            room = min(t - a, b - t)
            if best is None or room > best[0]:
                best = (room, pc)
    return best[1] if best and best[0] >= 0 else None


def window_pcm(root, pc, t):
    import kivi_replay_mine as K
    first, last = K.window_bounds(t, pc["start"], pc["n"], WIN_S)
    return np.fromfile(Path(root) / pc["clip"], dtype="<i2", count=last - first, offset=44 + 2 * first)


# ---------------------------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------------------------


def cmd_scan(a):
    import v6_room as R
    os.chdir(KH)
    heads = {n: R.Head(KH / f) for n, f in HEADS.items()}
    seen, pieces = set(), []
    for ix in INDEXES:
        p = KH / ix
        if not p.exists():
            continue
        for pc in json.loads(p.read_text())["pieces"]:
            if pc["clip"] in seen:
                continue
            seen.add(pc["clip"])
            pieces.append(pc)
    dets = defaultdict(list)
    hours = Counter()
    for k, pc in enumerate(pieces, 1):
        e = np.load(pc["emb"], mmap_mode="r")
        t0 = R._utc(pc["start_utc"]).timestamp()
        hours[(pc["device"], R.local_date(pc["start_utc"]))] += pc["samples"] / RATE / 3600
        for n, h in heads.items():
            steps, p = R.piece_probs(h, e)
            times = R.step_time(steps)
            for i in R.detections(times, p, SCAN_FLOOR):
                lo, hi = max(0, i - 12), min(len(p), i + 13)          # +-1 s around the first step above the floor
                j = lo + int(np.argmax(p[lo:hi]))
                dets[pc["device"]].append((t0 + float(times[j]), n, float(p[j])))
        print(f"[{k}/{len(pieces)}] {pc['device']} {R.local_date(pc['start_utc'])} {pc['samples'] / RATE / 3600:.2f} h",
              flush=True)
    moments = []
    for dev, xs in dets.items():
        for m in merge_detections(xs):
            day, ev = madrid(m["t"])
            moments.append({"device": dev, "t": m["t"], "day": day, "evening": ev, "scores": m["scores"]})
    moments.sort(key=lambda m: (m["t"], m["device"]))
    WORK.mkdir(mode=0o700, parents=True, exist_ok=True)
    out = {"heads": HEADS, "floor": SCAN_FLOOR, "pieces": len(pieces),
           "hours": {f"{d}|{day}": round(h, 2) for (d, day), h in sorted(hours.items())}, "moments": moments}
    path = WORK / "moments.json"
    path.write_text(json.dumps(out))
    os.chmod(path, 0o600)
    cnt = Counter((m["device"], m["day"], max(m["scores"].values()) >= P3_FLOOR) for m in moments)
    print(json.dumps({"pieces": len(pieces), "moments_ge_0.3": len(moments),
                      "moments_ge_0.5": sum(1 for m in moments if max(m["scores"].values()) >= P3_FLOOR),
                      "per_device_day_ge_0.5": {f"{d}|{day}": n for (d, day, hi), n in sorted(cnt.items()) if hi}}, indent=1))


# ---------------------------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------------------------


def existing_windows(root):
    """[(device, t, label, label_source, table name, id)] of every window table row, read fresh."""
    out = []
    for p in L.window_tables(root):
        for r in L.read_miss_table(p):
            try:
                out.append((r["device"], utc_epoch(r["utc"]), r["label"], r.get("label_source", ""), p.name, r["id"]))
            except ValueError:
                continue
    return out


def live_spans(rows):
    """{device: [(t0, t1)]}: the audio a live clip holds (6 s tail before the detection, the request after it)."""
    out = defaultdict(list)
    for r in rows:
        det = L.detection_epoch(r)
        out[r["device"]].append((det - L.TAIL_S - 0.5, det + float(r["stt_len"] or 0) + 0.5))
    return out


def live_runs(rows):
    """{device: sorted detection times} of every live run (wake or button) of the table."""
    out = defaultdict(list)
    for r in rows:
        out[r["device"]].append(L.detection_epoch(r))
    return {d: sorted(v) for d, v in out.items()}


def candidates(root, moments, fair_rows, live_rows, pieces):
    """Window candidates in tier order: [{tier, sources, device, t, day, evening, split, scores, piece}]."""
    runs = live_runs(live_rows)
    out = []
    for r in fair_rows:
        t = utc_epoch(r["utc"])
        near = [m for m in moments if m["device"] == r["device"] and abs(m["t"] - t) <= MERGE_S]
        sc = {}
        for m in near:
            for h, s in m["scores"].items():
                sc[h] = max(sc.get(h, 0.0), s)
        out.append({"tier": "P1", "sources": {"P1"}, "device": r["device"], "t": t, "scores": sc})
    p3, p4 = [], []
    for m in moments:
        top = max(m["scores"].values())
        follow = any(0.0 < x - m["t"] <= FOLLOW_S for x in runs.get(m["device"], ()))
        src = set()
        if top >= P3_FLOOR:
            src.add("P3")
        if top >= P4_FLOOR and follow:
            src.add("P4")
        if not src:
            continue
        c = {"tier": "P3" if "P3" in src else "P4", "sources": src, "device": m["device"], "t": m["t"],
             "scores": dict(m["scores"])}
        (p3 if c["tier"] == "P3" else p4).append(c)
    for c in out + p3 + p4:
        c["day"], c["evening"] = madrid(c["t"])
        c["split"] = split_of(c["day"])
        c["piece"] = piece_at(pieces, c["device"], c["t"])
    p3.sort(key=p3_order)
    p4.sort(key=lambda c: c["t"])
    out.sort(key=lambda c: c["t"])
    return out + p3 + p4


def dedup(cands, windows, spans, queued=None):
    """Drop what is labelled or queued already and merge candidates of one moment. Returns (kept, Counter of reasons).
    `queued` (a list) receives the pending window rows that already stand for a candidate: the plan moves them up to the
    candidate's tier instead of cutting a second window."""
    why = Counter()
    kept = []
    by_dev = defaultdict(list)
    for w in windows:
        by_dev[w[0]].append(w)
    for c in cands:
        dev, t = c["device"], c["t"]
        if c["piece"] is None:
            why[f"{c['tier']}: no local room piece"] += 1
            continue
        near = [w for w in by_dev[dev] if abs(w[1] - t) <= MATCH_S]
        definite = [w for w in near if w[2] and w[2] != "unclear"]
        if definite and not (c["tier"] == "P1" and all(w[2] == "unclear" for w in near if w[2])):
            why[f"{c['tier']}: labelled window within {MATCH_S:.0f} s"] += 1
            continue
        if c["tier"] != "P1" and [w for w in near if w[2] == "unclear"]:
            why[f"{c['tier']}: unclear window within {MATCH_S:.0f} s"] += 1
            continue
        pending = [w for w in near if not w[2]]
        if pending:
            why[f"{c['tier']}: already queued (pending window within {MATCH_S:.0f} s)"] += 1
            if queued is not None:
                queued.append({"tier": c["tier"], "table": pending[0][4], "ref": pending[0][5], "device": dev,
                               "t": t, "split": split_of(madrid(t)[0]), "evening": str(int(madrid(t)[1])),
                               "score": max(c["scores"].values() or [0.0])})
            continue
        if any(a <= t <= b for a, b in spans.get(dev, ())):
            why[f"{c['tier']}: inside a live clip"] += 1
            continue
        dup = next((k for k in kept if k["device"] == dev and abs(k["t"] - t) <= MERGE_S), None)
        if dup is not None:
            dup["sources"] |= c["sources"]
            why[f"{c['tier']}: same moment as an earlier candidate ({dup['tier']})"] += 1
            continue
        kept.append(c)
    return kept, why


class WindowLabeller:
    """Whisper (local, GPU) + ECAPA, as kivi_live_wakes autolabel loads them; nothing textual leaves `label`."""

    def __init__(self, root):
        import kivi_autolabel as A
        self.A = A
        self.tr = A.FasterWhisperTranscriber()
        self.emb = A.EcapaEmbedder()
        items, groups = A.enrol(A.enrolment_clips(root), self.emb)
        self.thr, rep = A.calibrate(items, groups)
        self.centr = A.centroids(items)
        print(f"enrolment: {dict(Counter(s for s, _ in items))}; leave-one-take-out {rep['correct']}/{rep['n']} correct, "
              f"{rep['unknown']} unknown, {rep['wrong']} wrong", flush=True)

    def label(self, pcm):
        A = self.A
        mid = pcm[int(MID[0] * RATE):int(MID[1] * RATE)]
        m_full = A.match_words(self.tr.transcribe(pcm))
        m_mid = A.match_words(self.tr.transcribe(mid)) if len(mid) >= RATE else None
        m = A.combine_matches(m_full, m_mid)
        ev = dict(m, speech_mid_s=A.speech_seconds(mid))
        label, conf = decide_window(ev)
        spk, spk_conf = "", 0.0
        if label in ("attempt", "other-speech", "unclear") and ev["speech_mid_s"] + (m["n_words"] > 0) > 0:
            seg = None
            if m_full["span"]:
                s0, s1 = m_full["span"]
                seg = pcm[max(0, int((s0 - 0.15) * RATE)):int((s1 + 0.15) * RATE)]
            if seg is None or len(seg) < int(0.4 * RATE):
                seg = A.speech_part(pcm, cap_s=WIN_S)
            spk, spk_conf = A.classify(A.sims_to(self.centr, self.emb.embed(seg)), self.thr)
        evidence = ";".join(f"{k}={v}" for k, v in (
            ("ph", int(m["phrase"])), ("weak", int(m["weak"])), ("kivi", int(m["kivi_exact"])), ("oye", int(m["oye_like"])),
            ("score", m["phrase_score"]), ("ksim", m["kivi_sim"]), ("words", m["n_words"]), ("wconf", m["mean_prob"]),
            ("spm", ev["speech_mid_s"])))
        return {"label": label, "conf": round(conf, 2), "speaker": spk, "speaker_conf": spk_conf, "evidence": evidence}

    def close(self):
        self.emb.close()


def read_csv(path, fields):
    p = Path(path)
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return [{k: (r.get(k) or "") for k in fields} for r in csv.DictReader(f)]


def backup(path):
    p = Path(path)
    if p.exists():
        b = p.with_name(p.name + ".bak-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        b.write_bytes(p.read_bytes())
        os.chmod(b, 0o600)
        return b
    return None


def cmd_build(a, labeller=None):
    root = Path(a.dataset_root).expanduser()
    moments = json.loads((WORK / "moments.json").read_text())["moments"]
    fair = read_csv(KH / FAIR_EVENTS, ("device", "utc", "madrid_day", "role", "evening", "models"))
    live_path = root / "live" / "live_wakes.csv"
    live_rows = L.read_table(live_path)
    pieces = list(pieces_from_manifests(root).values())
    table = root / "live-misses" / TABLE
    plan_dir = C.private_mkdir(root / "labelq", root)
    plan_path, key_path = plan_dir / "plan.csv", plan_dir / "key.csv"
    cur = L.read_miss_table(table)
    cands = candidates(root, moments, fair, live_rows, pieces)
    queued = []
    kept, why = dedup(cands, existing_windows(root), live_spans(live_rows), queued)
    print(f"candidates {len(cands)} (" + ", ".join(f"{t} {sum(1 for c in cands if c['tier'] == t)}" for t in TIERS)
          + f"); new after dedup {len(kept)}", flush=True)
    for k, n in sorted(why.items()):
        print(f"  dropped: {k}: {n}")
    hashes = set()
    for r in cur:
        p = root / "live-misses" / r["clip"]
        if p.exists():
            hashes.add(hashlib.sha256(p.read_bytes()[44:]).hexdigest())
    for p in L.window_tables(root):
        if p.name == TABLE:
            continue
        for r in L.read_miss_table(p):
            q = root / "live-misses" / r["clip"]
            if q.exists():
                hashes.add(hashlib.sha256(q.read_bytes()[44:]).hexdigest())
    if a.dry_run:
        print("dry run: nothing labelled or written")
        return
    own = labeller is None
    labeller = labeller or WindowLabeller(root)
    new_rows, dup_audio = [], 0
    n0 = len(cur)
    try:
        for i, c in enumerate(kept, 1):
            pcm = window_pcm(root, c["piece"], c["t"])
            h = hashlib.sha256(np.ascontiguousarray(pcm, "<i2").tobytes()).hexdigest()
            if h in hashes:
                dup_audio += 1
                continue
            hashes.add(h)
            res = labeller.label(pcm)
            utc = L.dir_stamp(c["t"])
            rel = f"{CLIPS}/{c['device']}/{utc}/window.wav"
            dst = root / "live-misses" / rel
            if not dst.exists():
                C.private_mkdir(dst.parent, root)
                C.write_wav_new(dst, pcm)
            import kivi_replay_mine as K
            rms, pkd = K.wav_levels(pcm)
            owner = to_owner_window(res["label"], res["conf"])
            row = {k: "" for k in FIELDS} | {
                "id": f"lq-{n0 + len(new_rows) + 1:05d}", "device": c["device"], "part": f"labelq-{c['tier']}", "utc": utc,
                "peak_v5": f"{c['scores'].get('v5', 0.0):.4f}" if c["scores"] else "", "rank": "",
                "level_dbfs": rms, "peak_dbfs": pkd, "clip": rel, "split": c["split"], "tier": c["tier"],
                "sources": "+".join(sorted(c["sources"])), "evening": str(int(c["evening"])),
                "peaks": ";".join(f"{k}={v:.3f}" for k, v in sorted(c["scores"].items())),
                "auto_evidence": res["evidence"] + f";auto={res['label']}/{res['conf']:.2f}/{res['speaker'] or '-'}"}
            if not owner:
                row.update(label=res["label"], label_source="auto-v1", label_conf=f"{res['conf']:.2f}",
                           speaker=res["speaker"] if res["label"] == "other-speech" else "",
                           speaker_conf=f"{res['speaker_conf']:.2f}" if res["label"] == "other-speech" else "")
            row["_auto"] = res
            new_rows.append(row)
            if i % 100 == 0:
                print(f"  {i}/{len(kept)} windows labelled", flush=True)
    finally:
        if own:
            labeller.close()
    autos = {r["id"]: r.pop("_auto") for r in new_rows}
    if new_rows:
        backup(table)
        fresh = L.read_miss_table(table)                 # fresh read right before the write: owner labels win
        L.write_miss_table(table, fresh + new_rows)
    print(f"windows written: {len(new_rows)}; identical audio dropped: {dup_audio}")
    write_plan(root, live_rows, new_rows, autos, plan_path, key_path, queued)


def write_plan(root, live_rows, new_rows, autos, plan_path, key_path, queued=()):
    """Plan = old plan items in their order, then the new items by tier (P1, P2, P3, P4). Key = auto label per item."""
    old_plan = read_csv(plan_path, PLAN_FIELDS)
    old_key = read_csv(key_path, KEY_FIELDS)
    have = {(p["kind"], p["ref"]) for p in old_plan}
    keyed = {(k["kind"], k["ref"]) for k in old_key}
    val_pending = {r["run_id"] for r in L.read_validation(root / "live" / L.VALIDATION_TABLE) if not r["label"]}
    items, keys = [], []
    # windows
    trusted_refs = []
    for r in new_rows:
        res = autos[r["id"]]
        owner = to_owner_window(res["label"], res["conf"])
        k = {"kind": "window", "ref": r["id"], "tier": r["tier"], "auto_label": res["label"], "auto_conf": f"{res['conf']:.2f}",
             "auto_speaker": res["speaker"], "audit": "0",
             "why": ("attempt" if res["label"] == "attempt" else "unclear" if res["label"] == "unclear" else
                     "low-confidence") if owner else ""}
        keys.append(k)
        if owner:
            items.append(_plan_item("window", r["id"], r["tier"], r["device"], r["utc"], r["split"], r["evening"]))
        else:
            trusted_refs.append(("window", r["id"]))
    # live clips (P2): rows without an owner label
    for r in sorted(live_rows, key=lambda r: (r["utc"], r["device"])):
        why = to_owner_live(r)
        if r["label"] and not r["label_source"].startswith("auto"):
            continue
        if ("live", r["run_id"]) in have or ("live", r["run_id"]) in keyed:
            continue
        day, ev = madrid(L.detection_epoch(r))
        keys.append({"kind": "live", "ref": r["run_id"], "tier": "P2", "auto_label": r["label"], "auto_conf": r["label_conf"],
                     "auto_speaker": r["speaker"], "audit": "0", "why": why or ""})
        if why and r["run_id"] not in val_pending:
            items.append(_plan_item("live", r["run_id"], "P2", r["device"], r["utc"], split_of(day), str(int(ev))))
        elif not why and r["label"] == "false" and r["run_id"] not in val_pending:
            trusted_refs.append(("live", r["run_id"]))
    # pending rows of other window tables that stand for a candidate: asked at the candidate's tier (no new window)
    for q in queued:
        if ("window", q["ref"]) in have or ("window", q["ref"]) in keyed or ("window", q["ref"]) in {(k["kind"], k["ref"]) for k in keys}:
            continue
        keys.append({"kind": "window", "ref": q["ref"], "tier": q["tier"], "auto_label": "", "auto_conf": "", "auto_speaker": "",
                     "audit": "0", "why": "queued-elsewhere"})
        items.append(dict(_plan_item("window", q["ref"], q["tier"], q["device"], L.dir_stamp(q["t"]), q["split"], q["evening"]),
                          table=q["table"]))
    sample = audit_sample([f"{k}|{ref}" for k, ref in trusted_refs])
    by_ref = {(k["kind"], k["ref"]): k for k in keys}
    win_rows = {r["id"]: r for r in new_rows}
    live_by = {r["run_id"]: r for r in live_rows}
    for s in sorted(sample):
        kind, ref = s.split("|", 1)
        by_ref[(kind, ref)].update(audit="1", why="audit")
        if kind == "window":
            r = win_rows[ref]
            items.append(_plan_item("window", ref, r["tier"], r["device"], r["utc"], r["split"], r["evening"]))
        else:
            r = live_by[ref]
            day, ev = madrid(L.detection_epoch(r))
            items.append(_plan_item("live", ref, "P2", r["device"], r["utc"], split_of(day), str(int(ev))))
    score = {r["id"]: row_score(r) for r in new_rows} | {q["ref"]: q.get("score", 0.0) for q in queued}
    items.sort(key=lambda it: plan_key(it, score.get(it["ref"], 0.0)))
    plan = old_plan + [it for it in items if (it["kind"], it["ref"]) not in have]
    for i, it in enumerate(plan, 1):
        it["order"] = str(i)
    backup(plan_path)
    backup(key_path)
    L.write_csv_atomic(plan_path, PLAN_FIELDS, plan)
    L.write_csv_atomic(key_path, KEY_FIELDS, old_key + [k for k in keys if (k["kind"], k["ref"]) not in keyed])
    print(f"plan: {len(plan)} item(s) ({len(plan) - len(old_plan)} new; audit sample {len(sample)} of {len(trusted_refs)} "
          f"trusted auto labels); {plan_path}")


def row_score(r):
    """Highest head score of a window row (`peaks` column), 0 when there is none."""
    vals = [float(x.split("=", 1)[1]) for x in (r.get("peaks") or "").split(";") if "=" in x]
    return max(vals or [0.0])


def plan_key(it, score=0.0):
    """Order of the plan: tier; inside P3 the p3_order blocks (c3-b evenings, c3-a evenings, c3-b days, c3-a days; even
    days first; highest score first); P1, P2 and P4 by time."""
    tier = TIERS.index(it["tier"])
    if it["tier"] != "P3":
        return (tier, 0, False, 0.0, it["utc"])
    block = (0 if it["evening"] == "1" else 2) + (0 if it["device"] == "c3-b" else 1)
    return (tier, block, it["split"] != "train", -score, it["utc"])


def cmd_reorder(a):
    """Put the plan in plan_key order again (scores from the window table, or the scanned moments for rows of other
    tables) and renumber; only the order changes."""
    root = Path(a.dataset_root).expanduser()
    plan_path = root / "labelq" / "plan.csv"
    plan = read_csv(plan_path, PLAN_FIELDS)
    win = {r["id"]: r for r in L.read_miss_table(root / "live-misses" / TABLE)}
    moments = json.loads((WORK / "moments.json").read_text())["moments"] if (WORK / "moments.json").exists() else []
    by_dev = defaultdict(list)
    for m in moments:
        by_dev[m["device"]].append(m)

    def score(it):
        if it["kind"] != "window":
            return 0.0
        if it["ref"] in win:
            return row_score(win[it["ref"]])
        t = utc_epoch(it["utc"])
        near = [max(m["scores"].values()) for m in by_dev[it["device"]] if abs(m["t"] - t) <= MATCH_S]
        return max(near or [0.0])
    plan.sort(key=lambda it: plan_key(it, score(it)))
    for i, it in enumerate(plan, 1):
        it["order"] = str(i)
    backup(plan_path)
    L.write_csv_atomic(plan_path, PLAN_FIELDS, plan)
    print(f"plan reordered: {len(plan)} item(s); tiers in order: "
          + ", ".join(f"{t} {sum(1 for it in plan if it['tier'] == t)}" for t in TIERS))


def _plan_item(kind, ref, tier, device, utc, split, evening):
    return {"order": "", "kind": kind, "table": TABLE if kind == "window" else "live_wakes.csv", "ref": ref, "tier": tier,
            "device": device, "utc": utc, "split": split, "evening": evening}


# ---------------------------------------------------------------------------------------------
# report and audit (fresh reads; counts only)
# ---------------------------------------------------------------------------------------------


def state(root):
    root = Path(root)
    plan = read_csv(root / "labelq" / "plan.csv", PLAN_FIELDS)
    key = {(k["kind"], k["ref"]): k for k in read_csv(root / "labelq" / "key.csv", KEY_FIELDS)}
    win = {r["id"]: r for r in L.read_miss_table(root / "live-misses" / TABLE)}
    live = {r["run_id"]: r for r in L.read_table(root / "live" / "live_wakes.csv")}
    return plan, key, win, live


def item_info(kind, ref, win, live, plan_row=None):
    if kind == "window" and ref not in win and plan_row:             # a pending row of another table (queued elsewhere)
        return plan_row["tier"], plan_row["split"], plan_row["device"], plan_row["evening"] == "1", "", "", ""
    if kind == "window":
        r = win.get(ref, {})
        return r.get("tier", ""), r.get("split", ""), r.get("device", ""), r.get("evening") == "1", r.get("label", ""), \
            r.get("label_source", ""), r.get("speaker", "")
    r = live.get(ref, {})
    day, ev = madrid(L.detection_epoch(r)) if r else ("", False)
    return "P2", split_of(day) if day else "", r.get("device", ""), ev, r.get("label", ""), r.get("label_source", ""), \
        r.get("speaker", "")


def cmd_report(a):
    root = Path(a.dataset_root).expanduser()
    plan, key, win, live = state(root)
    in_plan = {(p["kind"], p["ref"]): p for p in plan}
    rows = Counter()
    for (kind, ref), k in key.items():
        tier, split, dev, ev, lab, src, spk = item_info(kind, ref, win, live, in_plan.get((kind, ref)))
        auto = k["auto_label"]
        cell = f"{tier}|{split}"
        rows[(cell, "total")] += 1
        if auto in ("true", "attempt"):
            rows[(cell, "auto true/attempt")] += 1
        elif auto in ("false", "noise", "other-speech", "tv-media"):
            rows[(cell, "auto false")] += 1
        if (kind, ref) in in_plan:
            rows[(cell, "to owner")] += 1
            if k["audit"] == "1":
                rows[(cell, "of which audit")] += 1
    pend = len(L.plan_queue_items(root, lambda r: True, set())[0])
    out = {"per_source_split": {f"{c}": {m: n for (cc, m), n in sorted(rows.items()) if cc == c}
                                for c in sorted({c for c, _ in rows})},
           "plan_items": len(plan), "pending": pend,
           "owner_minutes_pending": round(pend * SECONDS_PER_ITEM / 60, 1)}
    # what the labels add to training (even days = train): trusted now, and what waits in the plan
    add = Counter()
    for (kind, ref), k in key.items():
        tier, split, dev, ev, lab, src, spk = item_info(kind, ref, win, live, in_plan.get((kind, ref)))
        if split != "train":
            continue
        owner = src in ("owner", "owner-button")
        if kind == "window":
            if lab == "other-speech" and dev == "c3-b" and ev:
                add["c3-b evening conversation windows, labelled (owner or trusted auto)"] += 1
            if not owner and (kind, ref) in in_plan and k["auto_label"] == "other-speech" and dev == "c3-b" and ev:
                add["c3-b evening conversation windows, waiting in the plan (audit)"] += 1
            if lab == "attempt" and owner:
                add[f"attempts among the mined room moments, owner-confirmed ({tier})"] += 1
            if not owner and k["auto_label"] == "attempt":
                add[f"attempts among the mined room moments, auto (waiting for the owner, {tier})"] += 1
            if not owner and k["auto_label"] == "unclear":
                add[f"unclear windows waiting for the owner ({tier})"] += 1
            if k["auto_label"] == "attempt" and k["auto_speaker"] == "spk02":
                add["spk02 attempt windows by auto speaker (owner confirms)"] += 1
        else:
            if lab == "true" and spk == "spk02":
                add["spk02 live calls (" + ("owner" if owner else "auto, owner confirms") + ")"] += 1
            if lab == "true" and spk == "spk01":
                add["spk01 live calls (" + ("owner" if owner else "auto, owner confirms") + ")"] += 1
            if lab in ("", "unclear", "not-a-wake") and not owner:
                add["live clips unclear / not-a-wake / unlabelled, waiting for the owner"] += 1
    out["training_even_days"] = dict(sorted(add.items()))
    print(json.dumps(out, indent=1))
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1))
        os.chmod(a.json, 0o600)


def cmd_audit(a):
    root = Path(a.dataset_root).expanduser()
    _plan, key, win, live = state(root)
    agree = Counter()
    for (kind, ref), k in key.items():
        if k["audit"] != "1":
            continue
        _, _, _, _, lab, src, _ = item_info(kind, ref, win, live)
        if src not in ("owner", "owner-button"):
            agree["not labelled yet"] += 1
            continue
        auto = k["auto_label"]
        if kind == "live":
            agree[f"live auto {auto} -> owner {lab}"] += 1
        else:
            agree[f"window auto {auto} -> owner {lab}"] += 1
    print(json.dumps(dict(sorted(agree.items())), indent=1))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["scan", "build", "report", "audit", "reorder"])
    ap.add_argument("--dataset-root", default=C.DEFAULT_DATASET_ROOT)
    ap.add_argument("--dry-run", action="store_true", help="(build) candidates and dedup counts only")
    ap.add_argument("--json", help="(report) also write the counts here")
    a = ap.parse_args(argv)
    {"scan": cmd_scan, "build": cmd_build, "report": cmd_report, "audit": cmd_audit, "reorder": cmd_reorder}[a.what](a)


if __name__ == "__main__":
    main()
