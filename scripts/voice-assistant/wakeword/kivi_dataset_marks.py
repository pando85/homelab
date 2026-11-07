#!/usr/bin/env python3
"""Owner-run: record a later decision about a session of the permanent dataset, without touching the
manifest or any audio.

    kivi_dataset_marks.py split --session 2026...-spk01-tv-c3a --split train --reason "first session trains"
    kivi_dataset_marks.py supersede --session 2026...-spk01-tv-c3a --revision "" --by r2 --reason "bad time mapping"

`split` appends to `<dataset>/splits.jsonl` (the last record for a session wins). `supersede` appends to
`<dataset>/superseded.jsonl`: rows of that session and extraction revision (and, with `--source`, only that
source) are wrong and are skipped by `read_manifest` consumers (`training_rows`, the scorers). Both files are
append-only, mode 600; nothing is deleted or rewritten. `--dry-run` prints the rows it would cover.
"""
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kivi_real_common as C  # noqa: E402


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=["split", "supersede"])
    ap.add_argument("--dataset-root", default=C.DEFAULT_DATASET_ROOT)
    ap.add_argument("--session", required=True)
    ap.add_argument("--split", choices=C.SPLITS, help="(split) the split of the whole session")
    ap.add_argument("--revision", default="", help="(supersede) the extraction revision to mark ('' is the default one)")
    ap.add_argument("--by", help="(supersede) the revision that replaces it")
    ap.add_argument("--source", help="(supersede) only this source")
    ap.add_argument("--reason", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    root = Path(a.dataset_root).expanduser()
    rows = [r for r in C.read_jsonl(root / "manifest.jsonl") if r.get("session") == a.session]
    if not rows:
        raise SystemExit(f"no manifest rows for session {a.session!r}")
    if a.what == "split":
        if not a.split:
            raise SystemExit("--split is required")
        rec = {"session": a.session, "split": a.split, "reason": a.reason, "set_utc": now()}
        target, covered = root / "splits.jsonl", len(rows)
    else:
        if a.by is None:
            raise SystemExit("--by is required: the revision that replaces the superseded one")
        covered = sum(1 for r in rows if (r.get("revision") or "") == a.revision and a.source in (None, r["source"]))
        if not covered:
            raise SystemExit(f"no rows of session {a.session!r} with revision {a.revision!r}")
        rec = {"session": a.session, "revision": a.revision, "source": a.source, "superseded_by": a.by,
               "reason": a.reason, "set_utc": now()}
        target = root / "superseded.jsonl"
    print(f"{a.what}: {covered} manifest row(s) of the session covered -> {target.name}")
    if not a.dry_run:
        C.append_jsonl(target, [rec])
        print("recorded")


if __name__ == "__main__":
    main()
