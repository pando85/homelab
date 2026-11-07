#!/usr/bin/env python3
"""Pick the fixed, stratified benchmark subset from the kivi-fake-v1 audio set and stage it.

Synthetic audio only (Piper voices, TV-content mixes at clean / +5 / 0 dB), see
docs/voice-assistant/experiments/kivi-decider-poc.md. Commands are stratified by phrasing style,
the other kinds (questions, out of domain, unclear, unsupported, multi) by kind; every picked
item is taken in all three conditions, so the comparison is paired per clip.

    python3 select_subset.py --data $POC/data/kivi-fake-v1 --out $STAGE [--seed 20261105]

Writes $STAGE/clips/<cond>/<id>.wav (copies) and $STAGE/manifest.csv
(id, cond, path, audio_s, kind, style). Nothing from a real household is involved.
"""
import argparse
import csv
import json
import random
import shutil
from pathlib import Path

CONDS = ["clean", "snr+5", "snr+0"]
PER_STYLE = 16  # commands per style (8 styles -> 128 commands)
PER_OTHER = {"question": 10, "other": 6, "unclear": 6, "unsupported": 5, "multi": 5}  # 32 items


def pick(items, per_style, per_other, seed):
    """Deterministic stratified sample of items that have audio."""
    rng = random.Random(seed)
    items = [i for i in items if i.get("audio")]
    out = []
    styles = sorted({i["style"] for i in items if i["kind"] == "command"})
    for s in styles:
        pool = sorted((i for i in items if i["kind"] == "command" and i["style"] == s), key=lambda i: i["id"])
        out += rng.sample(pool, min(per_style, len(pool)))
    for kind in sorted(per_other):
        pool = sorted((i for i in items if i["kind"] == kind), key=lambda i: i["id"])
        out += rng.sample(pool, min(per_other[kind], len(pool)))
    return sorted(out, key=lambda i: i["id"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="kivi-fake-v1 directory (items.jsonl, audio_fake.csv, audio/)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=20261105)
    a = ap.parse_args()
    data, out = Path(a.data), Path(a.out)
    items = [json.loads(line) for line in open(data / "items.jsonl", encoding="utf-8")]
    chosen = pick(items, PER_STYLE, PER_OTHER, a.seed)
    ids = {i["id"]: i for i in chosen}
    rows = []
    for r in csv.DictReader(open(data / "audio_fake.csv", encoding="utf-8")):
        if r["id"] in ids and r["cond"] in CONDS:
            src = data / "audio" / r["path"]
            rel = f"clips/{r['cond']}/{r['id']}.wav"
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, out / rel)
            rows.append([r["id"], r["cond"], rel, r["audio_s"], ids[r["id"]]["kind"], ids[r["id"]]["style"]])
    rows.sort()
    with open(out / "manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "cond", "path", "audio_s", "kind", "style"])
        w.writerows(rows)
    print(f"{len(chosen)} items, {len(rows)} clips -> {out}")


if __name__ == "__main__":
    main()
