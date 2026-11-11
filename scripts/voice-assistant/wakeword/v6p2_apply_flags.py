#!/usr/bin/env python3
"""Kivi v6.2: apply the data-quality flags (`/work/workspaces/kivi-wakeword/quality/`, 1b) to the v6 training arrays and
write filtered copies under v6p2/. Counts only.

- live positives: keep only `true` clips with trigger `wake` (drops the two mis-cut stt-path clips
  c3-b/20261107T235840.830Z and c3-b/20261107T235906.770Z); their before-Kivi/command windows are dropped too;
- live hard negatives: drop the 5 clips with a kivi-like word at the tail (3 sure, 2 hotword-only; an 08:24-08:26Z
  c3-b retry cluster);
- synthetic positives: drop voices vctk_tr (isolated and context), ona (isolated), north_m, alba, riccardo (context);
  keep half (seeded) of davefx (both), siwis and ona (context).
Real takes are rebuilt from a filtered takes.npz by `filter-takes` (drop 20261108T092940Z B1c9 on every source, B4c10
on the webcam) and v3_real.py feats.

    v6p2_apply_flags.py arrays
    v6p2_apply_flags.py filter-takes --src vnext/real/takes.npz --out v6p2/real/takes.npz
"""
import csv
import os
import sys
from pathlib import Path

import numpy as np

W = Path("/work/workspaces/kivi-wakeword")
DROP_LIVE_POS = set()   # 1b correction 2026-11-12: the two stt-path clips are correct and kept
DROP_LIVE_HARD = {"c3-b/20261108T082445.653Z", "c3-a/20261108T091213.284Z", "c3-b/20261108T100623.078Z",
                  "c3-b/20261108T082553.920Z", "c3-b/20261108T082635.313Z"}
DROP_VOICES_ISO = {"vctk_tr", "siwis", "upmc"}            # final 1b + OOF decision (2026-11-12)
HALF_VOICES_ISO = {"davefx", "ona"}
DROP_VOICES_CTX = {"vctk_tr", "siwis", "upmc", "riccardo"}
HALF_VOICES_CTX = {"davefx", "ona", "north_m", "alba"}
UPWEIGHT_CONFUSERS = ("kiri", "kili", "kibo", "gibi", "chivi")   # OOF: often at or above t_pos; windows doubled
DROP_TAKES = [("20261108T092940Z", 1, 9, None), ("20261108T092940Z", 4, 10, "webcam-mic")]


def save(dst, X, src=None):
    np.save(dst, X)
    os.chmod(dst, 0o600)
    if src is not None:
        Path(str(dst) + ".sources.txt").write_text("\n".join(src) + "\n")
    print(dst, X.shape, flush=True)


def filter_by_source(src_npy, keep_fn, dst):
    X = np.load(src_npy)
    src = Path(str(src_npy) + ".sources.txt").read_text().split()
    assert len(src) == len(X), src_npy
    m = np.array([keep_fn(s) for s in src])
    save(dst, X[m], [s for s, k in zip(src, m) if k])
    return int((~m).sum())


def voices(manifest):
    return {r["file"]: r["voice"] for r in csv.DictReader(open(manifest, encoding="utf-8"))}


def keep_voice(v, drop, half, name):
    """Drop a flagged voice; keep a deterministic half (crc32 of the clip name) of a down-weighted one."""
    import zlib
    if v in drop:
        return False
    if v in half:
        return zlib.crc32(name.encode()) % 2 == 0
    return True


def cmd_arrays():
    F, Q = W / "v6/feat", W / "v6p2/feat"
    Q.mkdir(parents=True, exist_ok=True)
    n = {}
    for spk in ("spk01", "spk02", "unk"):
        n[f"live_pos_{spk}"] = filter_by_source(F / f"live_pos_{spk}.npy", lambda s: s not in DROP_LIVE_POS,
                                                Q / f"live_pos_{spk}.npy")
    n["live_negpart"] = filter_by_source(F / "live_negpart.npy", lambda s: s not in DROP_LIVE_POS, Q / "live_negpart.npy")
    n["live_hard"] = filter_by_source(F / "live_hard.npy", lambda s: s not in DROP_LIVE_HARD, Q / "live_hard.npy")
    iso = voices(W / "vnext/clips/positive_train.manifest.csv")
    files = (F / "pos_train.npy.files.txt").read_text().split()
    X = np.load(F / "pos_train.npy", mmap_mode="r")
    assert len(files) == len(X)
    m = np.array([keep_voice(iso[f], DROP_VOICES_ISO, HALF_VOICES_ISO, f) for f in files])
    save(Q / "pos_train.npy", np.asarray(X[m]))
    (Q / "pos_train.npy.files.txt").write_text("\n".join(f for f, k in zip(files, m) if k) + "\n")
    n["pos_train"] = int((~m).sum())
    ctx = voices(W / "v6/clips_ctx/ctx_pos_train.manifest.csv")
    src = (F / "ctxp_syn_pos.npy.sources.txt").read_text().split()
    val = set(sorted(set(src))[-500:])          # the 500 context clips held out since v6.1 (stage-2 validation)
    n["ctxp_syn_pos_tr"] = filter_by_source(F / "ctxp_syn_pos.npy",
                                            lambda s: s not in val and keep_voice(ctx[s], DROP_VOICES_CTX, HALF_VOICES_CTX, s),
                                            Q / "ctxp_syn_pos_tr.npy")
    print({k: v for k, v in n.items()}, "windows dropped")


def cmd_filter_takes(src, out):
    T = {k: v for k, v in np.load(src).items()}
    n = len(T["audio"])
    keep = np.ones(n, bool)
    for i in range(n):
        for sess, b, c, source in DROP_TAKES:
            if str(T["session"][i]).startswith(sess) and int(T["block"][i]) == b and int(T["cue"][i]) == c and \
                    (source is None or str(T["source"][i]) == source):
                keep[i] = False
    np.savez(out, **{k: v[keep] if hasattr(v, "__len__") and len(v) == n else v for k, v in T.items()})
    os.chmod(out, 0o600)
    print(out, int(keep.sum()), "of", n, "takes kept")


def cmd_oof():
    """Grouped 5-fold out-of-fold flags (quality/oof_flags_*.csv). Real positives and real negatives are kept: the ASR
    check supports the phrase in every OOF-flagged real positive, and the real near-word takes are the confusers C2
    needs. Synthetic positives with an OOF median below 0.01 and synthetic negatives that a held-out fold accepts at the
    v6-matched operating point (n_above_fold_thr >= 1) are dropped (unintelligible or target-like renderings)."""
    Q = W / "v6p2/feat"
    pos = {(r["source"], r["item"]) for r in csv.DictReader(open(W / "quality/oof_flags_pos.csv", encoding="utf-8"))
           if r["flag"] == "1" and float(r["oof_median"]) < 0.01}
    neg = {(r["source"], r["item"]) for r in csv.DictReader(open(W / "quality/oof_flags_neg.csv", encoding="utf-8"))
           if int(r["n_above_fold_thr"] or 0) >= 1}
    n = {}
    files = (Q / "pos_train.npy.files.txt").read_text().split()
    X = np.load(Q / "pos_train.npy")
    m = np.array([("pos_train", f) not in pos for f in files])
    save(Q / "pos_train.npy", X[m])
    (Q / "pos_train.npy.files.txt").write_text("\n".join(f for f, k in zip(files, m) if k) + "\n")
    n["pos_train"] = int((~m).sum())
    n["ctxp_syn_pos_tr"] = filter_by_source(Q / "ctxp_syn_pos_tr.npy", lambda s: ("ctxp_syn_pos", s) not in pos,
                                            Q / "ctxp_syn_pos_tr.npy")
    files = (W / "v6/feat/near_train.npy.files.txt").read_text().split()
    X = np.load(W / "v6/feat/near_train.npy")
    text = {r["file"]: r["text"].lower() for r in csv.DictReader(open(W / "vnext/clips/negative_train.manifest.csv", encoding="utf-8"))}
    m = np.array([("near_train", f) not in neg for f in files])
    up = np.array([k and any(c in text.get(f, "") for c in UPWEIGHT_CONFUSERS) for f, k in zip(files, m)])
    save(Q / "near_train.npy", np.concatenate([X[m], X[up]]))
    n["near_train"] = int((~m).sum())
    n["near_train_doubled"] = int(up.sum())
    var = {r["file"]: r["variant"].lower() for r in csv.DictReader(open(W / "v6/clips_ctx/ctx_neg_train.manifest.csv", encoding="utf-8"))}
    n["ctxn_syn_conf"] = filter_by_source(W / "v6/feat/ctxn_syn_conf.npy", lambda s: ("ctxn_syn_conf", s) not in neg,
                                          Q / "ctxn_syn_conf.npy")
    Xc = np.load(Q / "ctxn_syn_conf.npy")
    sc = (Q / "ctxn_syn_conf.npy.sources.txt").read_text().split()
    upc = np.array([any(c in var.get(s, "") for c in UPWEIGHT_CONFUSERS) for s in sc])
    save(Q / "ctxn_syn_conf.npy", np.concatenate([Xc, Xc[upc]]), sc + [s for s, u in zip(sc, upc) if u])
    n["ctxn_syn_conf_doubled"] = int(upc.sum())
    n["ctxp_syn_negpart"] = filter_by_source(W / "v6/feat/ctxp_syn_negpart.npy", lambda s: ("ctxp_syn_negpart", s) not in neg,
                                             Q / "ctxp_syn_negpart.npy")
    print(n, "windows dropped by the OOF rule")


if __name__ == "__main__":
    if sys.argv[1] == "arrays":
        cmd_arrays()
    elif sys.argv[1] == "oof":
        cmd_oof()
    elif sys.argv[1] == "filter-takes":
        cmd_filter_takes(sys.argv[3], sys.argv[5])
