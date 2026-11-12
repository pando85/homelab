"""Local auto-labeller for the live "oye Kivi" wake clips (`kivi_live_wakes.py autolabel`).

Why: the owner does not want to listen to every clip. This fills the EMPTY label and speaker cells of
`live/live_wakes.csv`; an owner label (any non-empty cell without `label_source` auto-*) always wins and is never
changed. Run only on the owner's machine, nothing leaves it.

Privacy (rules of the owner, 2026-11-11):
  * Transcripts exist only as Python strings in memory while one clip is judged. They are never written to disk,
    printed, logged or sent anywhere, and the script prints no text, only counts. Speaker embeddings are computed
    transiently and never saved.
  * Speech to text runs LOCALLY (faster-whisper, large-v3-turbo, the model the cluster runs), not in the cluster:
    the cluster's Whisper logs every transcript to Loki for 180 days (temporary usage study, data register),
    which would store the transcripts of these clips. A Wyoming client is deliberately not offered.
  * Output per row: label, label_source (`auto-v1`), label_conf, speaker, speaker_conf and `auto_evidence`
    (booleans, match scores, model peaks and speech seconds; no text).

Trigger (column `trigger`, wake | button | unknown): the remote's mic button (push to talk) also closes a wake file and
starts a speech-to-text run, so some "wakes" are not detections. The model that was live (column `model`) is replayed
on the wake tail with the server's exact framing (`eval_stream.stream(..., n_total=)`: the 1280-sample grid anchored at
sample 0 of the full wake file, whose length comes from the `wake_len` column, 160-sample frames, final partial piece
included); if its raw score never reaches the live threshold in the last TRIGGER_LAST_S seconds it was not a wake
detection. With this replay a real detection crosses exactly, at the last embedding step (data audit 2026-11-11, 51 of 52
same-run tails), so there is no tolerance. c = `cross` (>= threshold: wake), `weak` (BUTTON_MAX to there: unknown) or
`none` (below BUTTON_MAX: `button`, or another trigger). A tail shorter than MIN_TAIL_S is `unknown`. Evidence (a): the request audio in the same run directory
(`same`) or from a separate stt-only run (`sep`, the `stt_run` column). Rows with trigger=button get the label
`not-a-wake` (0.9; 0.6 when the text says "oye Kivi" anyway, so the owner looks); an owner label on such a row is
kept and flagged `conflict` in `auto_evidence`.

Phrase position (columns phrase_end_s, continuous_command, phrase_in_stt, stt_phrase_start_s, stt_phrase_end_s; numbers
and booleans only): the clips hold "oye Kivi" followed at once by the command. `phrase_end_s` is where the phrase ends in
wake_tail.wav (seconds from its start): the end of the matched "Kivi" word from the in-memory word timestamps when the
text matched, else the first step of the last TRIGGER_LAST_S in which the live model crosses its threshold (an upper
bound: the model needs a moment after the word; `auto_evidence` says which, `pe_src=words|model`, and whether the two
agree, `pe_ok`). Training should cut a window that ends at phrase_end_s plus a small margin, never the command.
`continuous_command` is true when speech energy continues within 0.35 s after phrase_end_s (tail plus the start of
stt.wav when the request is in the same run directory, the tail alone for a separate stt-only run). `phrase_in_stt`
is true when "oye Kivi" is found in stt.wav itself (a button press followed by the natural phrase, or a repeated
phrase), with its start and end offsets in stt.wav.

Label rules (auto-v1; thresholds are constants below, tuned on the distribution of the first pass):
  phrase matched in the last 6 s (or the last 3 s) -> true, 0.92 (+0.03 for both passes, +0.03 when the detecting
      model's own peak is above its threshold, at most 0.98)
  "oye" + near-miss "kivi" (one edit), "kivi" alone, "oye" alone -> unclear 0.5 (owner)
  no oye-like and no kivi-like word:
      no words and almost no speech energy in the last 3 s -> false 0.95
      no words, speech energy present -> unclear 0.4
      words (unrelated) and the request was short (< SHORT_STT_S) -> false 0.8
      words (unrelated), mean word confidence >= 0.6 -> false 0.75, else unclear 0.45
What `review --only-uncertain` and the queue show to the owner (`is_uncertain`, `review_priority`): no label, auto
`unclear` and auto `not-a-wake` (always), an auto `true` whose speaker is unknown or whose confidence is below
UNCERTAIN_CONF. Auto `false` is trusted at any confidence (Phase 0 audit, 2026-11-12), auto `true` too.

Speaker: ECAPA-TDNN (speechbrain/spkrec-ecapa-voxceleb, Apache-2.0), cosine similarity against the centroid of each
enrolled speaker (spk01: the owner's real takes of the dataset manifest from tv-c3a and pc sources; spk02: her c3-b
button takes). Thresholds come from leave-one-out on the enrolment takes (see `calibrate`). Only the speech of a
clip is embedded: the span of the matched phrase (or the last 3 s of the tail) and the request audio. A low margin is
`unknown`; `spk-other` is only for a clip that resembles neither enrolled speaker at all. Rows labelled `false`
get speaker `unknown`.
"""
import os
import re
import shutil
import subprocess
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import kivi_real_common as C

RATE = C.RATE
SOURCE = "auto-v1"
SHORT_STT_S = 1.5
TRIGGER_LAST_S = 2.0
BUTTON_MAX = 0.5                     # below this raw score in the last TRIGGER_LAST_S nothing like a detection happened
MIN_TAIL_S = 1.0
TAIL_LAST_S = 3.0
UNCERTAIN_CONF = 0.8                 # an auto label below this goes to the owner
OYE_MIN_SIM = 0.67
KIVI_NEAR_SIM = 0.75
LOW_SPEECH_S = 0.3                   # less speech than this in the last 3 s: nothing was said
WORD_CONF_MIN = 0.6
WHISPER_MODEL = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
ECAPA_MODEL = "speechbrain/spkrec-ecapa-voxceleb"
# raw (uncalibrated) model files and the raw probability that equals the server's 0.5
MODEL_SPECS = {
    "oye_kivi": ("v4/final/oye_kivi_uncalibrated.tflite", 0.9437),
    "oye_kivi_next": ("releases/v5-oye-kivi-2026-11-08/model/oye_kivi_next_uncalibrated.tflite", 0.9754),
    "oye_kivi_ctx": ("releases/v6-oye-kivi-2026-11-11/model/oye_kivi_ctx_uncalibrated.tflite", 0.9595),
    "oye_kivi_ctx2": ("releases/v6-p2-oye-kivi-2026-11-12/model/oye_kivi_ctx2_uncalibrated.tflite", 0.9825),
    "oye_kivi_ctx3": ("releases/v6-p3-oye-kivi-2026-11-13/model/oye_kivi_ctx3_uncalibrated.tflite", 0.945),
}


# ---------------------------------------------------------------------------------------------
# text matching (pure; strings only live in the caller's memory)
# ---------------------------------------------------------------------------------------------


def fold(text):
    """Lower case, no accents, letters only, then a Spanish-ear phonetic fold: qu/c(a,o,u)/k -> k, v/w -> b, y -> i."""
    t = unicodedata.normalize("NFKD", str(text).lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r"[^a-z]", "", t)
    t = re.sub(r"qu", "k", t)
    t = re.sub(r"c(?=[aou])", "k", t)
    return t.replace("v", "b").replace("w", "b").replace("y", "i")


def lev(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def sim(a, b):
    if not a or not b:
        return 0.0
    return 1.0 - lev(a, b) / max(len(a), len(b))


KIVI = fold("kivi")                           # "kibi"
OYE = fold("oye")                             # "oie"
OYE_EXTRA = {"olle", "ole", "oi"}


def kivi_sim(t):
    t = fold(t)
    if len(t) > 4 and t.endswith("s"):
        t = t[:-1]
    return sim(t, KIVI)


def oye_sim(t):
    t = fold(t)
    return 1.0 if t in OYE_EXTRA else sim(t, OYE)


def match_words(words):
    """words: [(text, start_s, end_s, prob)] -> evidence dict about the wake phrase. Pure; keeps no text.

    kivi candidates are single words, two adjacent words joined ("ki" "vi") and the tail of a word that begins like
    "oye" ("oyekivi"). `phrase` needs an exact (folded) kivi and an oye-like word before it, at most two words apart.
    """
    toks = [(fold(w[0]), w) for w in words]
    toks = [(f, w) for f, w in toks if f]
    n = len(toks)
    kiv = []                                  # (index of first word, last index, sim)
    for i in range(n):
        kiv.append((i, i, kivi_sim(toks[i][1][0])))
        if i + 1 < n:
            kiv.append((i, i + 1, kivi_sim(toks[i][1][0] + toks[i + 1][1][0])))
    compound = []                             # "oyekivi" as one word: (index, oye sim of the head, kivi sim of the tail)
    for i, (f, _) in enumerate(toks):
        for cut in range(2, len(f) - 2):
            o, k = sim(f[:cut], OYE), sim(f[cut:], KIVI)
            if o >= OYE_MIN_SIM and k >= KIVI_NEAR_SIM:
                compound.append((i, o, k))
    oye = [oye_sim(t[1][0]) for t in toks]
    best_k = max([k[2] for k in kiv] + [c[2] for c in compound] + [0.0])
    best_o = max(oye + [c[1] for c in compound] + [0.0])
    phrase = weak = False
    span = None
    ph_score = 0.0
    for a, b, k in kiv:
        if k < KIVI_NEAR_SIM:
            continue
        before = [oye[j] for j in range(max(0, a - 2), a) if oye[j] >= OYE_MIN_SIM]
        if not before:
            continue
        o = max(before)
        j = max(range(max(0, a - 2), a), key=lambda q: oye[q])
        if k == 1.0:
            phrase = True
            if (o + k) / 2 > ph_score:
                ph_score = (o + k) / 2
                span = (toks[j][1][1], toks[b][1][2])
        else:
            weak = True
    for i, o, k in compound:
        if k == 1.0:
            phrase = True
            ph_score = max(ph_score, (o + k) / 2)
            span = span or (toks[i][1][1], toks[i][1][2])
        else:
            weak = True
    if phrase:
        weak = False
    return {"phrase": phrase, "weak": weak, "phrase_score": round(ph_score, 3), "kivi_sim": round(best_k, 3),
            "oye_sim": round(best_o, 3), "kivi_exact": best_k == 1.0, "oye_like": best_o >= OYE_MIN_SIM,
            "kivi_near": best_k >= KIVI_NEAR_SIM, "n_words": n,
            "mean_prob": round(float(np.mean([w[3] for _, w in toks])), 3) if toks else 0.0,
            "span": span}


def combine_matches(full, last):
    """Evidence from the 6 s pass and the last-3 s pass (either may be None)."""
    ms = [m for m in (full, last) if m is not None]
    out = {k: any(m[k] for m in ms) for k in ("phrase", "weak", "kivi_exact", "oye_like", "kivi_near")}
    out["phrase_both"] = bool(full and last and full["phrase"] and last["phrase"])
    out["phrase_score"] = max([m["phrase_score"] for m in ms] + [0.0])
    out["kivi_sim"] = max([m["kivi_sim"] for m in ms] + [0.0])
    out["oye_sim"] = max([m["oye_sim"] for m in ms] + [0.0])
    out["n_words"] = max([m["n_words"] for m in ms] + [0])
    out["mean_prob"] = max([m["mean_prob"] for m in ms] + [0.0])
    spans = [m["span"] for m in ms if m["span"]]
    out["span"] = spans[0] if spans else None
    return out


# ---------------------------------------------------------------------------------------------
# audio evidence
# ---------------------------------------------------------------------------------------------


def speech_seconds(pcm, last_s=None):
    """Seconds of speech-like energy (C.energy_segments) in the last `last_s` of `pcm` (all of it when None)."""
    x = pcm if last_s is None else pcm[-int(last_s * RATE):]
    if len(x) < RATE // 5:
        return 0.0
    segs = C.energy_segments(x, 0.0, margin_db=10.0, min_len_s=0.15, max_len_s=1e9)
    floor_db = 20 * np.log10(np.sqrt(np.mean((x.astype(np.float32) / 32768.0) ** 2)) + 1e-7)
    if floor_db < -62:                        # digital silence / dead mic: percentile floor would invent bursts
        return 0.0
    return round(sum(s["t1"] - s["t0"] for s in segs), 2)


def continuous_after(pcm, pe_s, win=(0.05, 0.35), margin_db=10.0, frame_s=0.02):
    """Whether speech-like energy continues right after `pe_s`: True / False, or None when `pcm` ends before the window
    (less than 5 frames of it). The floor is the 20th percentile of the frame energies of all of `pcm`."""
    n = int(frame_s * RATE)
    nf = len(pcm) // n
    if nf < 10:
        return None
    x = pcm[:nf * n].astype(np.float32).reshape(nf, n) / 32768.0
    db = 20 * np.log10(np.sqrt((x ** 2).mean(axis=1)) + 1e-7)
    act = db > float(np.percentile(db, 20.0)) + margin_db
    a, b = int((pe_s + win[0]) / frame_s), min(nf, int((pe_s + win[1]) / frame_s))
    if b - a < 5:
        return None
    return bool(act[a:b].mean() >= 0.3)


def speech_part(pcm, cap_s=8.0, min_s=0.4):
    """The speech-like samples of a clip joined together (at most cap_s of them); the whole clip if none found."""
    segs = C.energy_segments(pcm, 0.0, margin_db=10.0, min_len_s=0.15, max_len_s=1e9)
    parts, total = [], 0.0
    for s in segs:
        a, b = int(s["t0"] * RATE), int(s["t1"] * RATE)
        parts.append(pcm[a:b])
        total += (b - a) / RATE
        if total >= cap_s:
            break
    x = np.concatenate(parts) if parts else pcm
    x = x[:int(cap_s * RATE)]
    return x if len(x) >= int(min_s * RATE) else pcm[:int(cap_s * RATE)]


# ---------------------------------------------------------------------------------------------
# label rules
# ---------------------------------------------------------------------------------------------


def decide_label(ev):
    """ev: combined text evidence plus speech_last3_s, stt_len, own_peak, own_thr -> (label, confidence)."""
    if ev["phrase"]:
        conf = 0.92 + (0.03 if ev.get("phrase_both") else 0) + (0.03 if ev.get("own_peak", 0) >= ev.get("own_thr", 2) else 0)
        return "true", round(min(conf, 0.98), 2)
    if ev["weak"] or ev["oye_like"] or ev["kivi_near"]:
        return "unclear", 0.5
    if ev["n_words"] == 0:
        if ev["speech_last3_s"] < LOW_SPEECH_S:
            return "false", 0.95
        return "unclear", 0.4
    if ev["stt_len"] < SHORT_STT_S:
        return "false", 0.8
    if ev["mean_prob"] >= WORD_CONF_MIN:
        return "false", 0.75
    return "unclear", 0.45


def is_uncertain(row):
    """A row the owner should still listen to. Policy of 2026-11-12 (Phase 0 audit of the autolabel, 40-clip blind sample,
    docs/plan/2026-11-false-wake-plan.md): auto `true` and auto `false` are trusted as labels (owner agreed with 10 of 10
    and 13 of 13, none of the 13 auto-false a real attempt, 8 of them at confidence 0.75); auto `unclear` (the owner
    found 5 true attempts and 4 false wakes in 10) and auto `not-a-wake` (3 of 4 were false wakes) are not, so they always
    go to the owner. Also asked: no label, an auto `true` with an unknown speaker (the speaker is needed for per-speaker
    training and counts) and an auto `true` with confidence below UNCERTAIN_CONF. Owner labels are never uncertain."""
    if not row.get("label"):
        return True
    if not str(row.get("label_source", "")).startswith("auto"):
        return False
    if row["label"] in ("unclear", "not-a-wake"):
        return True
    if row["label"] == "false":
        return False
    try:
        conf = float(row.get("label_conf") or 0)
    except ValueError:
        conf = 0.0
    return conf < UNCERTAIN_CONF or row.get("speaker", "") in ("", "unknown")


def review_priority(row):
    """Order of the uncertain clips in the queue (0 first): auto unclear (about half are real attempts, so they decide
    the true and false counts), auto not-a-wake (button clips, most were false wakes), unlabelled, then an auto true that
    only lacks a speaker or confidence."""
    lab = row.get("label", "")
    if not lab:
        return 2
    return {"unclear": 0, "not-a-wake": 1}.get(lab, 3)


# ---------------------------------------------------------------------------------------------
# speaker
# ---------------------------------------------------------------------------------------------


def unit(v):
    v = np.asarray(v, dtype=np.float64)
    return v / (np.linalg.norm(v) + 1e-12)


def centroids(items):
    """items: [(speaker, embedding)] -> {speaker: unit centroid}."""
    by = {}
    for s, e in items:
        by.setdefault(s, []).append(unit(e))
    return {s: unit(np.mean(v, axis=0)) for s, v in by.items()}


def sims_to(centr, emb):
    e = unit(emb)
    return {s: float(c @ e) for s, c in centr.items()}


def classify(sims, thr):
    """sims {speaker: cosine} -> (speaker, confidence). thr: t_accept, t_margin, t_other."""
    if not sims:
        return "unknown", 0.0
    order = sorted(sims.items(), key=lambda kv: -kv[1])
    best_s, best = order[0]
    second = order[1][1] if len(order) > 1 else -1.0
    margin = best - second if len(order) > 1 else best - thr["t_accept"]
    if best >= thr["t_accept"] and margin >= thr["t_margin"]:
        conf = 0.5 + 0.25 * min(1.0, (margin - thr["t_margin"]) / 0.3) + 0.25 * min(1.0, (best - thr["t_accept"]) / 0.3)
        return best_s, round(min(conf, 0.99), 2)
    if best < thr["t_other"]:
        return "spk-other", 0.6
    return "unknown", 0.0


def calibrate(items, groups=None):
    """Leave-one-out on the enrolment takes. items: [(speaker, embedding)], groups: optional [group id] per item for
    a leave-one-group-out (session) check. Returns (thresholds, report). The thresholds are picked on the
    leave-one-take-out similarities: t_margin and t_accept give the most assignments at a precision of at least 98 %
    among the assigned takes (the rest is `unknown`; live far-field clips are harder than enrolment takes, so no looser); t_other is the 2nd percentile of the best similarity of held-out takes (a clip below
    it resembles nobody enrolled)."""
    n = len(items)
    rows = []
    for i in range(n):
        rest = [it for j, it in enumerate(items) if j != i]
        sims = sims_to(centroids(rest), items[i][1])
        rows.append((items[i][0], sims))
    best_sims = [max(s.values()) for _, s in rows]
    t_other = float(np.percentile(best_sims, 2)) - 0.05
    cand_a = np.round(np.linspace(0.0, 0.9, 46), 2)
    cand_m = np.round(np.linspace(0.0, 0.4, 41), 3)
    chosen = None                             # (precision ok, coverage, strictness): most assignments at >= 98 % precision, then the strictest
    for ta in cand_a:
        for tm in cand_m:
            thr = {"t_accept": float(ta), "t_margin": float(tm), "t_other": t_other}
            res = [classify(s, thr)[0] for _, s in rows]
            assigned = [(r, t) for r, (t, _) in zip(res, rows) if r not in ("unknown", "spk-other")]
            if not assigned:
                continue
            prec = sum(r == t for r, t in assigned) / len(assigned)
            key = (prec >= 0.98, len(assigned) if prec >= 0.98 else prec, ta + tm)
            if chosen is None or key > chosen[0]:
                chosen = (key, thr)
    thr = chosen[1] if chosen else {"t_accept": 0.5, "t_margin": 0.1, "t_other": t_other}
    res = [classify(s, thr)[0] for _, s in rows]
    per = {}
    for r, (t, _) in zip(res, rows):
        c = per.setdefault(t, Counter())
        c["n"] += 1
        c["correct"] += r == t
        c["unknown"] += r in ("unknown", "spk-other")
        c["wrong"] += r not in (t, "unknown", "spk-other")
    rep = {"n": n, "correct": sum(c["correct"] for c in per.values()),
           "unknown": sum(c["unknown"] for c in per.values()), "wrong": sum(c["wrong"] for c in per.values()),
           "per_speaker": {k: dict(v) for k, v in per.items()},
           "mean_best_sim": round(float(np.mean(best_sims)), 3)}
    if groups is not None:
        ok = tot = wrong = unk = 0
        for g in set(groups):
            train = [it for it, gg in zip(items, groups) if gg != g]
            if len({s for s, _ in train}) < 2:
                continue                      # a speaker with a single group cannot be held out
            cen = centroids(train)
            for it, gg in zip(items, groups):
                if gg == g and it[0] in cen:
                    r = classify(sims_to(cen, it[1]), thr)[0]
                    tot += 1
                    ok += r == it[0]
                    unk += r in ("unknown", "spk-other")
                    wrong += r not in (it[0], "unknown", "spk-other")
        rep["leave_group_out"] = {"n": tot, "correct": ok, "unknown": unk, "wrong": wrong}
    return thr, rep


# ---------------------------------------------------------------------------------------------
# backends (real ones import their libraries lazily; tests pass fakes)
# ---------------------------------------------------------------------------------------------


class FasterWhisperTranscriber:
    """transcribe(pcm16) -> [(text, start_s, end_s, prob)], in memory, language es, local model, GPU."""

    def __init__(self, model=WHISPER_MODEL, device="cuda", compute_type="int8_float16"):
        from faster_whisper import WhisperModel
        self.m = WhisperModel(model, device=device, compute_type=compute_type)

    def transcribe(self, pcm):
        x = pcm.astype(np.float32) / 32768.0
        segs, _ = self.m.transcribe(x, language="es", beam_size=5, vad_filter=True, word_timestamps=True,
                                    condition_on_previous_text=False, temperature=0.0)
        words = []
        for s in segs:
            if s.no_speech_prob > 0.8 and s.avg_logprob < -1.0:
                continue                      # a hallucination over silence
            for w in s.words or []:
                words.append((w.word, float(w.start), float(w.end), float(w.probability)))
        return words


class EcapaEmbedder:
    """embed(pcm16) -> unit vector (192). Transient: never saved."""

    def __init__(self, device="cuda"):
        import torch
        from huggingface_hub import snapshot_download
        from speechbrain.pretrained import EncoderClassifier
        self.torch = torch
        src = snapshot_download(ECAPA_MODEL)
        save = Path(os.environ.get("TMPDIR", "/tmp")) / f"ecapa-{os.getpid()}"
        self.m = EncoderClassifier.from_hparams(source=src, savedir=str(save), run_opts={"device": device},
                                              overrides={"pretrained_path": src})
        self.device = device
        self.save = save

    def embed(self, pcm):
        torch = self.torch
        x = torch.from_numpy(pcm.astype(np.float32) / 32768.0).unsqueeze(0).to(self.device)
        with torch.no_grad():
            e = self.m.encode_batch(x).squeeze().detach().cpu().numpy()
        return unit(e)

    def close(self):
        shutil.rmtree(self.save, ignore_errors=True)


class ModelScorer:
    """peaks(pcm16, n_total) -> {model: (peak over the tail, over the last 3 s, over the last TRIGGER_LAST_S, t, raw)} with
    the server's exact framing (raw, uncalibrated probabilities; `n_total` = samples of the full wake file the tail ends,
    see eval_stream.stream); t is the time (s from the tail start) of the first step in the last TRIGGER_LAST_S at or
    above the live threshold, else of the highest step there, and raw is the score of that step."""

    def __init__(self, work):
        from eval_stream import load_models
        self.specs = [f"{n}={Path(work) / rel}" for n, (rel, _) in MODEL_SPECS.items()]
        self.feats, self.models = load_models(self.specs)

    def peaks(self, pcm, n_total=None):
        from eval_stream import stream
        probs, times = stream(self.feats, self.models, pcm, n_total=n_total)
        t_end = len(pcm) / RATE
        out = {}
        for n, p in probs.items():
            def top(sec):
                q = p[times >= t_end - sec] if len(times) else p
                return float(q.max()) if len(q) else 0.0
            thr = MODEL_SPECS.get(n, (None, 2.0))[1]
            tc = raw = None
            if len(times):
                idx = np.flatnonzero(times >= t_end - TRIGGER_LAST_S)
                if len(idx):
                    q = p[idx]
                    hit = np.flatnonzero(q >= thr)
                    j = idx[hit[0]] if len(hit) else idx[int(np.argmax(q))]
                    tc, raw = float(times[j]), float(p[j])
            out[n] = (float(p.max()) if len(p) else 0.0, top(TAIL_LAST_S), top(TRIGGER_LAST_S), tc, raw)
        return out


def wake_total(row, tail_len):
    """Samples of the full wake file the tail is the end of (from the `wake_len` column, exact: the length is a multiple
    of 160 samples); the tail's own length when the column is empty, unreadable or shorter than the tail."""
    from eval_stream import wake_samples
    try:
        n = wake_samples(row.get("wake_len") or 0)
    except (TypeError, ValueError):
        n = 0
    return max(n, tail_len)


# ---------------------------------------------------------------------------------------------
# enrolment
# ---------------------------------------------------------------------------------------------


def enrolment_clips(root):
    """[(speaker, path, group)] from the manifest: spk01 = segments from the tv-c3a and pc-mic sources, spk02 = the
    c3-b button takes; superseded, excluded rows and clips above 4 s of the segment kind dropped."""
    rows = C.read_manifest(root)
    out = []
    for r in rows:
        if r.get("superseded") or r.get("excluded"):
            continue
        p = Path(root) / r["clip"]
        if not p.exists():
            continue
        if r.get("speaker") == "spk01" and r.get("kind") == "segment" and r.get("source") in ("tv-c3a", "pc-mic"):
            out.append(("spk01", p, str(r.get("session"))))
        elif r.get("speaker") == "spk02" and r.get("origin") == "button" and r.get("source") == "tv-c3b":
            out.append(("spk02", p, str(r.get("session"))))
    return out


def enrol(clips, embedder, max_len_s=4.0):
    items, groups = [], []
    for spk, p, g in clips:
        pcm = C.read_pcm16(p)
        if len(pcm) > max_len_s * RATE and spk == "spk01":
            continue                          # a long "segment" is not a single take
        items.append((spk, embedder.embed(speech_part(pcm, cap_s=max_len_s))))
        groups.append(g)
    return items, groups


# ---------------------------------------------------------------------------------------------
# trigger: wake-word detection or something else (the mic button)
# ---------------------------------------------------------------------------------------------


def trigger_of(model, stt_run, peaks, tail_s):
    """(trigger, a, c): a = `same` (request audio in the wake run's directory) or `sep` (separate stt-only run);
    c = `cross` (the live model's raw score reached its live threshold in the last TRIGGER_LAST_S, exact replay),
    `weak` (at least BUTTON_MAX), `none`, or `n/a`. trigger = wake for cross, button for none, else unknown."""
    a = "sep" if stt_run else "same"
    if model not in MODEL_SPECS or model not in peaks or tail_s < MIN_TAIL_S:
        return "unknown", a, "n/a"
    thr, p2 = MODEL_SPECS[model][1], peaks[model][2]
    if p2 >= thr:
        return "wake", a, "cross"
    if p2 >= BUTTON_MAX:
        return "unknown", a, "weak"
    return "button", a, "none"


# ---------------------------------------------------------------------------------------------
# per-clip analysis
# ---------------------------------------------------------------------------------------------


def analyse_clip(row, clip_dir, transcriber, embedder, scorer, centr, thr):
    """Returns a dict: label, label_conf, speaker, speaker_conf, trigger, cross (a, c cells) and evidence (string).
    Nothing textual leaves this function."""
    tail = C.read_pcm16(Path(clip_dir) / "wake_tail.wav")
    stt_path = Path(clip_dir) / "stt.wav"
    stt = C.read_pcm16(stt_path) if stt_path.exists() else np.zeros(0, dtype=np.int16)
    last = tail[-int(TAIL_LAST_S * RATE):]
    m_full = match_words(transcriber.transcribe(tail))
    m_last = match_words(transcriber.transcribe(last)) if len(tail) > len(last) else None
    m = combine_matches(m_full, m_last)
    pk = scorer.peaks(tail, wake_total(row, len(tail))) if scorer else {}
    own = pk.get(row["model"], (0.0, 0.0, 0.0))
    trig, ta, tc = trigger_of(row["model"], row.get("stt_run", ""), pk, len(tail) / RATE)
    span = None                               # the matched phrase in the tail, seconds from the tail start
    if m_full["span"]:
        span = m_full["span"]
    elif m_last and m_last["span"]:
        off = (len(tail) - len(last)) / RATE
        span = (m_last["span"][0] + off, m_last["span"][1] + off)
    t_model = own[3] if len(own) > 3 else None
    stt_m = match_words(transcriber.transcribe(stt)) if len(stt) >= int(0.5 * RATE) else None
    own_thr = MODEL_SPECS.get(row["model"], (None, 2.0))[1]
    ev = dict(m, speech_last3_s=speech_seconds(tail, TAIL_LAST_S), stt_len=float(row["stt_len"] or 0),
              own_peak=own[1], own_thr=own_thr)
    label, conf = decide_label(ev)
    if trig == "button":
        label, conf = "not-a-wake", (0.6 if m["phrase"] else 0.9)
    spk, spk_conf = "unknown", 0.0
    if label not in ("false", "not-a-wake") and centr:
        embs = []
        if span:
            seg = tail[max(0, int((span[0] - 0.15) * RATE)):int((span[1] + 0.15) * RATE)]
            if len(seg) >= int(0.4 * RATE):
                embs.append(embedder.embed(seg))
        if not embs:
            embs.append(embedder.embed(speech_part(last, cap_s=3.0)))
        if len(stt) >= int(0.6 * RATE):
            embs.append(embedder.embed(speech_part(stt, cap_s=8.0)))
        per = [sims_to(centr, e) for e in embs]
        avg = {s: float(np.mean([p[s] for p in per])) for s in centr}
        spk, spk_conf = classify(avg, thr)
    pos = {"phrase_end_s": "", "continuous_command": "", "phrase_in_stt": "false", "stt_phrase_start_s": "",
           "stt_phrase_end_s": ""}
    pe_src = pe_ok = ""
    if label == "true" or trig == "wake":
        pe = span[1] if span else t_model
        pe_src = "words" if span else ("model" if t_model is not None else "")
        if span and t_model is not None:
            pe_ok = int(span[1] - 0.1 <= t_model <= span[1] + 1.5)
        if pe is not None:
            pos["phrase_end_s"] = f"{pe:.2f}"
            audio = tail if ta == "sep" else np.concatenate([tail, stt[:int(3 * RATE)]])
            c = continuous_after(audio, pe)
            pos["continuous_command"] = "" if c is None else str(c).lower()
    if stt_m is not None and stt_m["phrase"] and stt_m["span"]:
        pos.update(phrase_in_stt="true", stt_phrase_start_s=f"{stt_m['span'][0]:.2f}",
                   stt_phrase_end_s=f"{stt_m['span'][1]:.2f}")
    evidence = ";".join(f"{k}={v}" for k, v in (
        ("ph", int(m["phrase"])), ("weak", int(m["weak"])), ("kivi", int(m["kivi_exact"])), ("oye", int(m["oye_like"])),
        ("score", m["phrase_score"]), ("ksim", m["kivi_sim"]), ("words", m["n_words"]),
        ("v4", round(pk.get("oye_kivi", (0, 0, 0))[0], 3)), ("v5", round(pk.get("oye_kivi_next", (0, 0, 0))[0], 3)),
        ("own2", round(own[2], 3)), ("pe_src", pe_src), ("pe_ok", pe_ok), ("trig", f"{trig}/{ta}/{tc}"),
        ("sp3", ev["speech_last3_s"]), ("stt", row["stt_len"])))
    return {"label": label, "label_conf": conf, "speaker": spk, "speaker_conf": spk_conf, "trigger": trig,
            "cross": (ta, tc), "phrase": bool(m["phrase"]), "evidence": evidence, **pos}


# ---------------------------------------------------------------------------------------------
# table update (merge, never overwrite)
# ---------------------------------------------------------------------------------------------


def review_running(pgrep=None):
    """True when a `kivi_live_wakes.py review` or `queue` process of this user is running."""
    if pgrep is not None:
        return bool(pgrep())
    r = subprocess.run(["pgrep", "-f", r"kivi_live_wakes\.py (review|queue)"], capture_output=True, text=True)
    mine = [p for p in r.stdout.split() if p.isdigit() and int(p) != os.getpid()]
    return bool(mine)


def wait_for_review(pgrep=None, sleep=time.sleep, poll_s=30, max_s=900, out=print):
    waited = 0
    while review_running(pgrep):
        if waited >= max_s:
            return False
        if waited == 0:
            out("a review process is running: not writing the table; waiting (poll 30 s, up to 15 min)")
        sleep(poll_s)
        waited += poll_s
    return True


POS_FIELDS = ("phrase_end_s", "continuous_command", "phrase_in_stt", "stt_phrase_start_s", "stt_phrase_end_s")


def merge_results(rows, results, redo=False):
    """Fill empty cells of `rows` (fresh from disk) from results {run_id: dict}. Returns counts. An owner cell
    (non-empty, label_source not auto-*) is never touched; with `redo` a row whose label_source is auto-* is recomputed."""
    n = Counter()
    for r in rows:
        res = results.get(r["run_id"])
        if not res:
            continue
        auto_label = str(r.get("label_source", "")).startswith("auto")
        if not r["label"] or (redo and auto_label):
            r["label"], r["label_source"], r["label_conf"] = res["label"], SOURCE, f"{res['label_conf']:.2f}"
            n["label"] += 1
        elif r["label"] and not auto_label:
            n["label_owner_kept"] += 1
        auto_speaker = bool(r.get("speaker_conf")) and auto_label
        if not r["speaker"] or (redo and auto_speaker):
            r["speaker"] = "unknown" if r["label"] in ("false", "not-a-wake") else res["speaker"]
            r["speaker_conf"] = f"{res['speaker_conf']:.2f}"
            n["speaker"] += 1
        if res.get("trigger") and (not r.get("trigger") or redo):
            r["trigger"] = res["trigger"]
        for k in POS_FIELDS:
            if k in res and (not r.get(k) or redo):
                r[k] = res[k]
        if not r.get("auto_evidence") or redo:
            r["auto_evidence"] = res["evidence"]
        if res.get("trigger") == "button" and r["label"] not in ("", "not-a-wake") \
                and not str(r.get("label_source", "")).startswith("auto"):
            r["auto_evidence"] = (r.get("auto_evidence") or "") + ";conflict=owner-label-vs-button"
            n["conflict"] += 1
    return n


def agreement(rows, results):
    """Auto result vs owner cells already present (owner = non-empty without label_source auto-*)."""
    out = Counter()
    for r in rows:
        res = results.get(r["run_id"])
        if not res or str(r.get("label_source", "")).startswith("auto"):
            continue
        if r["label"]:
            out["owner_labels"] += 1
            out["label_agree"] += res["label"] == r["label"]
            out["label_auto_unclear"] += res["label"] == "unclear"
            out["label_disagree"] += res["label"] not in (r["label"], "unclear")
        if r["speaker"]:
            out["owner_speakers"] += 1
            out["speaker_agree"] += res["speaker"] == r["speaker"]
            out["speaker_auto_unknown"] += res["speaker"] == "unknown"
    return out


# ---------------------------------------------------------------------------------------------
# trigger-only pass (no transcription, no speaker embedding): the exact replay of the live model on the tail
# ---------------------------------------------------------------------------------------------

def evidence_get(evidence, key):
    for part in str(evidence or "").split(";"):
        k, _, v = part.partition("=")
        if k == key:
            return v
    return None


def evidence_set(evidence, updates, drop=()):
    """auto_evidence is `k=v;k=v;...`: replace the value of the keys in `updates` where present (appended when missing),
    remove the keys in `drop`; every other part keeps its place and text."""
    parts = [p for p in str(evidence or "").split(";") if p]
    seen = set()
    out = []
    for p in parts:
        k = p.partition("=")[0]
        if k in drop:
            continue
        if k in updates:
            out.append(f"{k}={updates[k]}")
            seen.add(k)
        else:
            out.append(p)
    out += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    return ";".join(out)


def trigger_only(row, clip_dir, scorer):
    """The model-derived cells of one row from the exact replay of its tail; None when the row has no tail file."""
    tp = Path(clip_dir) / "wake_tail.wav"
    if not tp.exists():
        return None
    tail = C.read_pcm16(tp)
    pk = scorer.peaks(tail, wake_total(row, len(tail)))
    trig, ta, tc = trigger_of(row["model"], row.get("stt_run", ""), pk, len(tail) / RATE)
    own = pk.get(row["model"])
    res = {"trigger": trig, "cross": (ta, tc), "trig": f"{trig}/{ta}/{tc}", "t_model": None, "peak_t_s": "",
           "peak_raw": ""}
    for k, n in (("v4", "oye_kivi"), ("v5", "oye_kivi_next")):
        if n in pk:
            res[k] = f"{round(pk[n][0], 3)}"
    if own is not None:
        res["own2"] = f"{round(own[2], 3)}"
        if len(own) > 4 and own[3] is not None:
            res["t_model"] = own[3]
            res["peak_t_s"], res["peak_raw"] = f"{own[3]:.2f}", f"{own[4]:.4f}"
    if res["t_model"] is not None:                 # phrase end from the model, for rows that had no word timestamps
        res["phrase_end_s"] = f"{res['t_model']:.2f}"
        stt_path = Path(clip_dir) / "stt.wav"
        stt = C.read_pcm16(stt_path) if stt_path.exists() else np.zeros(0, dtype=np.int16)
        audio = tail if ta == "sep" else np.concatenate([tail, stt[:int(3 * RATE)]])
        c = continuous_after(audio, res["t_model"])
        res["continuous_command"] = "" if c is None else str(c).lower()
    return res


def merge_trigger_only(rows, results):
    """Update `rows` (fresh from disk) from trigger_only results {run_id: dict}. Changes only: trigger, peak_t_s, peak_raw,
    the model-derived keys of auto_evidence (v4, v5, own2, trig), phrase_end_s and continuous_command where pe_src is
    `model`, and the flags `conflict=` (an owner label on a button row; the stale flag is dropped when the row is not
    button any more) and `relabel=trigger-changed` (an auto label that rested on the old trigger). Labels, speakers and
    every other cell stay. Returns counts."""
    n = Counter()
    for r in rows:
        res = results.get(r["run_id"])
        if not res:
            continue
        n["rows"] += 1
        old = r.get("trigger", "")
        r["trigger"] = res["trigger"]
        n["trigger_changed"] += old != res["trigger"]
        r["peak_t_s"], r["peak_raw"] = res["peak_t_s"], res["peak_raw"]
        ev = r.get("auto_evidence", "")
        auto = str(r.get("label_source", "")).startswith("auto")
        if ev:
            upd = {k: res[k] for k in ("v4", "v5", "own2", "trig") if k in res}
            if evidence_get(ev, "pe_src") == "model" and "phrase_end_s" in res:
                r["phrase_end_s"] = res["phrase_end_s"]
                r["continuous_command"] = res["continuous_command"]
                n["phrase_end_from_model"] += 1
            ev = evidence_set(ev, upd)
            owner_conflict = bool(r.get("label")) and not auto and r["label"] != "not-a-wake" and res["trigger"] == "button"
            if owner_conflict:
                if evidence_get(ev, "conflict") is None:
                    ev = evidence_set(ev, {"conflict": "owner-label-vs-button"})
                    n["conflict_added"] += 1
            elif evidence_get(ev, "conflict") is not None and res["trigger"] != "button":
                ev = evidence_set(ev, {}, drop=("conflict",))
                n["conflict_dropped"] += 1
            lab = r.get("label", "")
            if auto and lab and ((lab == "not-a-wake" and res["trigger"] != "button")
                                 or (lab != "not-a-wake" and res["trigger"] == "button")):
                if evidence_get(ev, "relabel") is None:
                    ev = evidence_set(ev, {"relabel": "trigger-changed"})
                    n["relabel"] += 1
            r["auto_evidence"] = ev
        if bool(r.get("label")) and not auto and r["label"] != "not-a-wake" and res["trigger"] == "button":
            n["owner_conflicts"] += 1
        if evidence_get(r.get("auto_evidence"), "relabel") is not None:
            n["relabel_total"] += 1
    return n


def utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
