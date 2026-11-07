#!/usr/bin/env python3
"""Import a phone voice memo of "kivi" takes into the permanent real-voice dataset.

    kivi_import_phone.py MEMO.m4a --speaker spk02 --consent-confirmed \
        --split train [--near-labels kiwi,kiwi,tivi,tivi,kili,kili] [--expect 30] [--dry-run]

The memo (m4a, ogg, wav, anything ffmpeg reads) is converted to 16 kHz mono, the takes are found by
short-time energy (no model, no transcription), each take is cut with padding, and the clips are
labelled `kivi` (or `near:<word>` for the trailing near-word takes, in the order given), stored
as source `phone` with the speaker id and the split given with `--split` (required), and appended to the manifest. The
original file and the converted memo are kept whole under `<session>/phone/raw/`.

Rules: the speaker must have agreed to this (`--consent-confirmed` is required and recorded);
the speaker is a pseudonymous id (spk01...), never a name; nothing is played, transcribed or
printed; only counts are shown. The dataset is permanent: nothing is deleted or overwritten. A
phone memo is not an evaluation channel. Decided policy (2026-11-07): the memo of spk02 is `train`
for the production model; her button takes on c3-b are `test` and never trained on; the unseen-speaker
check is an ablation that excludes ALL spk02 data from training (`training_rows(exclude_speakers=...)`).
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kivi_real_common as C  # noqa: E402

RATE = C.RATE


def decode_memo(path):
    """int16 16 kHz mono samples of an audio file, through ffmpeg (no temporary file)."""
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(RATE),
                        "-f", "s16le", "-"], capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise SystemExit(f"ffmpeg could not read {path}: {r.stderr.decode(errors='replace')[:200]}")
    return np.frombuffer(r.stdout[:len(r.stdout) // 2 * 2], dtype="<i2").copy()


def find_takes(pcm, margin_db=10.0, min_len_s=0.15, max_len_s=3.0, merge_gap_s=0.3):
    segs = C.energy_segments(pcm, margin_db=margin_db, min_len_s=min_len_s, max_len_s=max_len_s,
                             merge_gap_s=merge_gap_s)
    takes = [s for s in segs if not s["long"]]
    return takes, len(segs) - len(takes)


def assign_labels(n, near_labels, near_from=None):
    """kivi for every take except the last len(near_labels), which get near:<label> in order."""
    labels = ["kivi"] * n
    if near_labels:
        k = len(near_labels)
        if k > n:
            raise SystemExit(f"{k} near labels but only {n} takes were found")
        if near_from is not None and n - near_from + 1 != k:
            raise SystemExit(f"--near-from {near_from} leaves {n - near_from + 1} takes but {k} near labels were given")
        for i, w in enumerate(near_labels):
            labels[n - k + i] = f"near:{w.lower()}"
    return labels


def import_memo(a):
    if not a.consent_confirmed and not a.dry_run:
        raise SystemExit("refusing: the speaker's consent is required before any of their data is imported "
                         "(pass --consent-confirmed once it is given)")
    C.valid_speaker(a.speaker)
    if a.split not in C.SPLITS:
        raise SystemExit(f"--split must be one of {C.SPLITS}")
    memo = Path(a.memo).expanduser()
    pcm = decode_memo(memo)
    takes, dropped = find_takes(pcm, a.margin_db, a.min_len_s, a.max_len_s)
    near = [w.strip() for w in a.near_labels.split(",")] if a.near_labels else []
    labels = assign_labels(len(takes), near, a.near_from)
    n_near = sum(l.startswith("near:") for l in labels)
    print(f"memo {len(pcm) / RATE:.1f} s: {len(takes)} takes found ({len(takes) - n_near} kivi, {n_near} near-word), "
          f"{dropped} bursts longer than {a.max_len_s:g} s skipped"
          + (f"; expected {a.expect}" if a.expect else ""))
    if a.expect and a.expect != len(takes):
        print(f"WARNING: expected {a.expect} takes but found {len(takes)}. Re-run with --margin-db / --min-len-s "
              "adjusted, or check the memo; nothing was written." if not a.force else
              f"WARNING: expected {a.expect} takes but found {len(takes)}; importing anyway (--force).")
        if not a.force:
            return None
    if a.dry_run:
        return None
    now = datetime.now(timezone.utc)
    sid = f"phone-{now:%Y%m%dT%H%M%SZ}-{a.speaker}"
    root = Path(a.dataset_root).expanduser()
    sroot = C.private_mkdir(root / sid, root)
    raw = C.private_mkdir(sroot / "phone" / "raw", root)
    orig = memo.read_bytes()
    C.write_bytes_new(raw / ("original" + memo.suffix.lower()), orig)   # the file's own name is not kept
    C.write_wav_new(raw / "memo-16k.wav", pcm)
    pad_b, pad_a = a.pad_before, a.pad_after
    exid = "ex-" + now.strftime("%Y%m%dT%H%M%S%fZ")
    rows = []
    for i, (t, lab) in enumerate(zip(takes, labels), 1):
        # never reach into a neighbouring take
        lo = takes[i - 2]["t1"] + (t["t0"] - takes[i - 2]["t1"]) / 2 if i > 1 else 0.0
        hi = t["t1"] + (takes[i]["t0"] - t["t1"]) / 2 if i < len(takes) else len(pcm) / RATE
        c0, c1 = max(lo, t["t0"] - pad_b), min(hi, t["t1"] + pad_a)
        rel = f"{sid}/phone/clips/00-phone-memo/c{i:03d}-{C.ascii_slug(lab)}.wav"
        C.private_mkdir(root / Path(rel).parent, root)
        C.write_wav_new(root / rel, pcm[int(c0 * RATE):int(c1 * RATE)])
        row = C.make_row(
            origin="phone", session=sid, source="phone", device="phone", session_device="", speaker=a.speaker,
            split=a.split, eval_channel=False, condition="phone-memo", conditions={}, tags={}, block=1,
            block_name="phone-memo", label=lab, word=lab.split(":", 1)[1] if ":" in lab else "kivi", kind="cue",
            clip=rel, start_s=round(c0, 3), end_s=round(c1, 3), cue_index=i, snr_db=t["snr_db"],
            extract_id=exid, consent_confirmed=True, revision="", needs_review=False)
        C.append_jsonl(root / "manifest.jsonl", [row])      # one fsynced line per clip, right after the clip
        rows.append(row)
    C.write_json_new(sroot / "session.json", {
        "schema": 1, "session_id": sid, "kind": "phone-memo", "device": "phone", "tv_source": "",
        "speaker": a.speaker, "split": a.split, "consent_confirmed": True, "imported_utc": now.isoformat(),
        "memo": {"sha256": hashlib.sha256(orig).hexdigest(), "bytes": len(orig), "seconds": round(len(pcm) / RATE, 2)},
        "takes": [{"index": i, "label": l, "start_s": round(t["t0"], 3), "end_s": round(t["t1"], 3)}
                  for i, (t, l) in enumerate(zip(takes, labels), 1)], "blocks": []})
    print(f"wrote {len(rows)} clips to {sroot} and appended them to {root / 'manifest.jsonl'}")
    return sroot


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("memo")
    ap.add_argument("--speaker", required=True, help="pseudonymous id, e.g. spk02 (never a name)")
    ap.add_argument("--consent-confirmed", action="store_true", help="the speaker agreed to this import")
    ap.add_argument("--split", required=True, choices=C.SPLITS,
                    help="required, no default: the owner decides (policy: the production memo of spk02 is train)")
    ap.add_argument("--dataset-root", default=C.DEFAULT_DATASET_ROOT)
    ap.add_argument("--near-labels", help="comma list for the last takes, in order: kiwi,kiwi,tivi,tivi")
    ap.add_argument("--near-from", type=int, help="1-based index of the first near-word take (checked)")
    ap.add_argument("--expect", type=int, help="number of takes you recorded; refuses to import on a mismatch")
    ap.add_argument("--force", action="store_true", help="import even if --expect does not match")
    ap.add_argument("--margin-db", type=float, default=10.0, help="burst threshold above the memo's noise floor")
    ap.add_argument("--min-len-s", type=float, default=0.15)
    ap.add_argument("--max-len-s", type=float, default=3.0)
    ap.add_argument("--pad-before", type=float, default=1.0)
    ap.add_argument("--pad-after", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true", help="only count the takes")
    import_memo(ap.parse_args(argv))


if __name__ == "__main__":
    main()
