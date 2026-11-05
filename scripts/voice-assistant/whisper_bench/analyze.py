#!/usr/bin/env python3
"""Offline analysis of the benchmark results: latency, WER, CER, command exact match, paired tests.

    python3 analyze.py --results out1 out2 --items items.jsonl --base int8-b0 \
        [--gpu out1/summary.json] [--order int8-b0,int8-b5,...]

--results takes directories (results-<config>.jsonl files) or single .jsonl files; --items is the
kivi-fake-v1 items.jsonl (reference text and kind per id). Prints Markdown tables.
Metrics: see metrics.py. Pairing is per (id, condition) clip against --base.
"""
import argparse
import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import metrics as m  # noqa: E402

CONDS = ["clean", "snr+5", "snr+0"]


def load_results(path):
    """{config: [row, ...]} from a results-*.jsonl file or a directory of them; skips bad lines."""
    path = Path(path)
    files = sorted(path.glob("results-*.jsonl")) if path.is_dir() else [path]
    out = {}
    for f in files:
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if "config" in r and "id" in r and "latency_s" in r:
                out.setdefault(r["config"], []).append(r)
    return out


def load_manifest(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    for r in rows:
        r["audio_s"] = float(r["audio_s"])
    return rows


def load_items(path, ids=None):
    items = {}
    for line in open(path, encoding="utf-8"):
        i = json.loads(line)
        if ids is None or i["id"] in ids:
            items[i["id"]] = {"kind": i["kind"], "text": i["text"], "style": i.get("style")}
    return items


def summarize(rows, items):
    """Per condition and 'all': n, latency stats, corpus WER and CER (all items), command exact."""
    out = {}
    for cond in CONDS + ["all"]:
        rs = [r for r in rows if cond == "all" or r["cond"] == cond]
        if not rs:
            continue
        lat = [r["latency_s"] for r in rs]
        we = wn = ce = cn = 0
        ek = en = 0
        for r in rs:
            ref = items[r["id"]]["text"]
            a, b = m.word_errors(ref, r["text"])
            c, d = m.char_errors(ref, r["text"])
            we, wn, ce, cn = we + a, wn + b, ce + c, cn + d
            if items[r["id"]]["kind"] == "command":
                en += 1
                ek += m.exact_match(ref, r["text"])
        out[cond] = {"n": len(rs), "lat_p50": m.percentile(lat, 50), "lat_p90": m.percentile(lat, 90),
                     "lat_max": max(lat), "lat_mean": sum(lat) / len(lat),
                     "wer": we / wn if wn else float("nan"), "cer": ce / cn if cn else float("nan"),
                     "exact": (ek, en)}
    return out


def paired(base_rows, other_rows, items, n_boot=10000):
    """Paired comparison on the clips both configs have: McNemar on command exact match and a
    paired bootstrap of the latency median, p90 and mean difference (other minus base)."""
    b = {(r["id"], r["cond"]): r for r in base_rows}
    o = {(r["id"], r["cond"]): r for r in other_rows}
    out = {}
    for cond in CONDS + ["all"]:
        keys = sorted(k for k in b if k in o and (cond == "all" or k[1] == cond))
        if not keys:
            continue
        cmd = [k for k in keys if items[k[0]]["kind"] == "command"]
        bok = [m.exact_match(items[k[0]]["text"], b[k]["text"]) for k in cmd]
        ook = [m.exact_match(items[k[0]]["text"], o[k]["text"]) for k in cmd]
        fixed, broken, p = m.mcnemar_exact(bok, ook)
        bl, ol = [b[k]["latency_s"] for k in keys], [o[k]["latency_s"] for k in keys]
        same = sum(m.normalize(b[k]["text"]) == m.normalize(o[k]["text"]) for k in keys)
        out[cond] = {"n": len(keys), "fixed": fixed, "broken": broken, "mcnemar_p": p,
                     "same_text": same,
                     "d_p50": m.paired_bootstrap(bl, ol, "median", n_boot),
                     "d_p90": m.paired_bootstrap(bl, ol, "p90", n_boot),
                     "d_mean": m.paired_bootstrap(bl, ol, "mean", n_boot)}
    return out


def fmt_ci(t, unit="s"):
    return f"{t[0]:+.3f} [{t[1]:+.3f}, {t[2]:+.3f}]"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", nargs="+", required=True)
    ap.add_argument("--items", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--order", default="")
    ap.add_argument("--gpu", nargs="*", default=[], help="summary.json files (peak memory)")
    ap.add_argument("--boot", type=int, default=10000)
    ap.add_argument("--quiet", default="", help="live_intervals.json (from live_overlap.py): drop the clips that "
                    "overlapped a request on the live Whisper, per config")
    ap.add_argument("--margin", type=float, default=1.0, help="seconds added around each clip for --quiet")
    a = ap.parse_args()
    res = {}
    for p in a.results:
        res.update(load_results(p))
    if a.quiet:
        import live_overlap
        iv = json.load(open(a.quiet))
        for n in list(res):
            bad = live_overlap.overlapped(res[n], iv, a.margin)
            res[n] = [r for r in res[n] if (r["id"], r["cond"]) not in bad]
    ids = {r["id"] for rows in res.values() for r in rows}
    items = load_items(a.items, ids)
    names = [n for n in a.order.split(",") if n in res] or sorted(res)
    mem = {}
    for p in a.gpu:
        for run in json.load(open(p))["runs"]:
            mem[run["name"]] = run
    sums = {n: summarize(res[n], items) for n in names}

    print("### Summary per condition\n")
    print("| Config | Cond | n | p50 s | p90 s | max s | WER % | CER % | Command exact |")
    print("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for n in names:
        for c in CONDS + ["all"]:
            s = sums[n].get(c)
            if s:
                k, t = s["exact"]
                print(f"| {n} | {c} | {s['n']} | {s['lat_p50']:.3f} | {s['lat_p90']:.3f} | {s['lat_max']:.3f} | "
                      f"{100 * s['wer']:.2f} | {100 * s['cer']:.2f} | {k}/{t} ({100 * k / t:.1f} %) |")
    if mem:
        print("\n### GPU memory (nvidia-smi, whole card, MiB)\n")
        print("| Config | Before start | After warm-up | Peak while measuring | Own use at peak |")
        print("| --- | ---: | ---: | ---: | ---: |")
        for n in names:
            r = mem.get(n)
            if r and r.get("peak_mem_mib"):
                print(f"| {n} | {r['base_mem_mib']} | {r['mem_after_warmup_mib']} | {r['peak_mem_mib']} | "
                      f"{r['peak_mem_mib'] - r['base_mem_mib']} |")
    print(f"\n### Paired against `{a.base}` (other minus base; 95 % bootstrap CI)\n")
    print("| Config | Cond | n | same text | exact: fixed / broken | McNemar p | d p50 s | d p90 s | d mean s |")
    print("| --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- |")
    for n in names:
        if n == a.base:
            continue
        for c, d in paired(res[a.base], res[n], items, a.boot).items():
            print(f"| {n} | {c} | {d['n']} | {d['same_text']} | {d['fixed']} / {d['broken']} | {d['mcnemar_p']:.3f} | "
                  f"{fmt_ci(d['d_p50'])} | {fmt_ci(d['d_p90'])} | {fmt_ci(d['d_mean'])} |")


if __name__ == "__main__":
    main()
