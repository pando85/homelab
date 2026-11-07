#!/usr/bin/env python3
"""Find the benchmark clips that overlapped a request on the LIVE Whisper (shared GPU).

The live server logs "Completed request in S second(s)" for every request; only its timestamp
and duration are used here (the transcript lines are never read). A live request occupies the
GPU roughly from (completion - S) to completion; a benchmark clip from (t - latency) to t.
The runner records `t` (epoch seconds, UTC) per clip.

    kubectl logs deploy/home-assistant-whisper --timestamps | python3 live_overlap.py --extract > live.json
    (library use) overlapped(clip_rows, live_intervals, margin_s)
"""
import datetime as dt
import json
import re
import sys

LINE = re.compile(r"^(\S+)\s.*Completed request in ([0-9.]+) second")


def parse_intervals(lines):
    """[(start_epoch, end_epoch)] of live requests from timestamped log lines (any UTC offset)."""
    out = []
    for line in lines:
        m = LINE.match(line)
        if not m:
            continue
        ts = m.group(1)
        ts = re.sub(r"(\.\d{6})\d*", r"\1", ts)  # nanoseconds -> microseconds
        end = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        out.append((end - float(m.group(2)), end))
    return sorted(out)


def overlapped(rows, intervals, margin_s=1.0):
    """Set of (id, cond) of clips whose [t - latency, t] window, widened by margin_s, touches a
    live request interval."""
    bad = set()
    for r in rows:
        a, b = r["t"] - r["latency_s"] - margin_s, r["t"] + margin_s
        if any(s < b and e > a for s, e in intervals):
            bad.add((r["id"], r["cond"]))
    return bad


if __name__ == "__main__":
    if "--extract" in sys.argv:
        json.dump(parse_intervals(sys.stdin), sys.stdout)
