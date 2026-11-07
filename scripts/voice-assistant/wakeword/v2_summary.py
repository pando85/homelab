#!/usr/bin/env python3
"""Tables for the Kivi v1 / v2 comparison from `fast_report.py --json` outputs (aggregates only).

    v2_summary.py --report v1=v1.json --op v1=0.98 --report v2=v2.json --op v2=0.95 \
        --dev mls_es_dev --out table.md

Recall labels expected in the reports: clean, snr10, snr0 (held-out voices) and nearmiss (near-words).
Every `--long` label except `--dev` counts towards the test total of false accepts.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eval_stream import poisson_ci  # noqa: E402


def at(rows, t, group="all"):
    for r in rows:
        if abs(r["threshold"] - t) < 1e-9 and r.get("group", "all") == group:
            return r
    raise KeyError(t)


def fa_total(rep, t, dev):
    k = h = 0
    for label, rows in rep["long"].items():
        if label == dev:
            continue
        r = at(rows, t)
        k += r["false_accepts"]
        h += r["hours"]
    return k, h


def pct(r):
    return f"{100 * r['rate']:.1f} % [{100 * r['ci95'][0]:.1f}, {100 * r['ci95'][1]:.1f}]"


def fa_str(k, h):
    lo, hi = poisson_ci(k, h)
    return f"{k} = {k / h:.2f}/h [{lo:.2f}, {hi:.2f}]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="append", required=True, help="NAME=fast_report.json")
    ap.add_argument("--op", action="append", default=[], help="NAME=threshold (operating point)")
    ap.add_argument("--dev", default="mls_es_dev")
    ap.add_argument("--out")
    a = ap.parse_args()
    reps = {k: json.load(open(v)) for k, v in (s.split("=", 1) for s in a.report)}
    ops = {k: float(v) for k, v in (s.split("=", 1) for s in a.op)}
    L = []
    L.append("| Model @ threshold | Clean recall | TV 10 dB | TV 0 dB | Near-word acceptance | "
             "False accepts, test total | MLS dev (selection) |")
    L.append("| --- | --- | --- | --- | --- | --- | --- |")
    for name, rep in reps.items():
        t = ops.get(name)
        if t is None:
            continue
        c = rep["clips"]
        k, h = fa_total(rep, t, a.dev)
        dv = at(rep["long"][a.dev], t) if a.dev in rep["long"] else None
        L.append(f"| {name} @ {t} | {pct(at(c['clean'], t))} | {pct(at(c['snr10'], t))} | "
                 f"{pct(at(c['snr0'], t))} | {pct(at(c['nearmiss'], t))} | {fa_str(k, h)} | "
                 f"{dv['false_accepts'] if dv else '-'} in {dv['hours'] if dv else '-'} h |")
    L.append("")
    for name, rep in reps.items():
        L.append(f"**{name}: threshold trade-off** (test total {fa_total(rep, rep['thresholds'][0], a.dev)[1]:.1f} h)")
        L.append("")
        L.append("| Threshold | Clean | TV 10 dB | TV 0 dB | Near-words | False accepts (test) | MLS dev |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for t in rep["thresholds"]:
            c = rep["clips"]
            k, h = fa_total(rep, t, a.dev)
            dv = at(rep["long"][a.dev], t)["false_accepts"] if a.dev in rep["long"] else "-"
            L.append(f"| {t} | {100 * at(c['clean'], t)['rate']:.1f} % | {100 * at(c['snr10'], t)['rate']:.1f} % | "
                     f"{100 * at(c['snr0'], t)['rate']:.1f} % | {100 * at(c['nearmiss'], t)['rate']:.1f} % | "
                     f"{k} = {k / h:.2f}/h | {dv} |")
        L.append("")
    s = "\n".join(L)
    print(s)
    if a.out:
        open(a.out, "w").write(s + "\n")


if __name__ == "__main__":
    main()
