#!/usr/bin/env python3
"""Dry-run conversation-agent evaluation for the Kivi voice assistant.

Sends Spanish test phrases (phrases_es.yaml) to any OpenAI-compatible
/v1/chat/completions endpoint (Kiais, Ollama's /v1, ...) with streaming, a
Spanish system prompt, a tool set modelled on Home Assistant's Assist LLM API
and a FAKE home (inventory_es.yaml). Tool calls are recorded and graded, never
executed: a plausible fake result is fed back so the model produces the final
spoken answer. Per-phrase JSON goes under a git-ignored directory outside this repository (default
~/webos-voice-satellite/devices/private/kivi); a summary table is printed and written next to it.

Examples:
  python3 scripts/voice-assistant/agent_eval/run.py plan
  AGENT_EVAL_API_KEY=... python3 scripts/voice-assistant/agent_eval/run.py run \\
      --base-url https://isidoro.grigri.cloud/v1 --model fast --label kiais-fast
  python3 scripts/voice-assistant/agent_eval/run.py run --base-url http://OLLAMA:11434/v1 \\
      --model qwen2.5:7b --label ollama-qwen2.5-7b --warmup
  python3 scripts/voice-assistant/agent_eval/run.py summarize ~/webos-voice-satellite/devices/private/kivi/kiais-fast/*

The API key comes only from the environment (--api-key-env) and is never
written to any result file. Never commit the output directory.
"""
import argparse
import datetime
import glob
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import time
import urllib.parse

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import client  # noqa: E402
import grade  # noqa: E402
import ha_fake  # noqa: E402

REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))  # homelab repo root
# Private results never go into git: default to a git-ignored directory of the sibling
# webos-voice-satellite checkout (override with --out or AGENT_EVAL_OUT).
DEFAULT_OUT = os.environ.get("AGENT_EVAL_OUT") or os.path.expanduser(
    "~/webos-voice-satellite/devices/private/kivi")
MAX_ROUNDS = 4
CATEGORIES = ("control", "general", "live", "followup")


# --- phrases -----------------------------------------------------------------

def load_phrases(path=None):
    with open(path or os.path.join(HERE, "phrases_es.yaml"), encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    items = []
    for cat in CATEGORIES:
        for p in data.get(cat, []):
            turns = p["turns"] if "turns" in p else [{"text": p["text"], "expect": p["expect"]}]
            items.append({"id": p["id"], "category": cat, "kind": p.get("kind", cat), "turns": turns})
    return items


def plan_items(items, categories, repeat_control, sample, seed, ids=None):
    """Expand phrases into (item, rep) jobs; reps interleaved to spread time effects."""
    chosen = [i for i in items if i["category"] in categories and (not ids or i["id"] in ids)]
    if sample and sample < len(chosen):
        rnd = random.Random(seed)
        chosen = sorted(rnd.sample(chosen, sample), key=lambda i: items.index(i))
    jobs = []
    reps = max(1, repeat_control)
    for rep in range(1, reps + 1):
        for it in chosen:
            if rep == 1 or it["category"] == "control":
                jobs.append((it, rep))
    return jobs


def worst_case_requests(jobs):
    """Typical cost: one request per turn, one more when a tool is called."""
    return sum(len(it["turns"]) * (2 if it["category"] in ("control", "followup", "live") else 1) for it, _ in jobs)


# --- conversation runner -----------------------------------------------------

def run_conversation(item, home, tools, send, budget):
    """Run one phrase (all its turns). `send(messages)` does one streaming request."""
    messages = [{"role": "system", "content": home.system_prompt()}]
    turns = []
    for spec in item["turns"]:
        messages.append({"role": "user", "content": spec["text"]})
        rounds, final_text, error = [], "", None
        for _ in range(MAX_ROUNDS):
            budget.take()
            r = send(messages)
            calls_rec = []
            round_rec = {"ttft_s": r["ttft_s"], "ttft_any_s": r["ttft_any_s"], "ttfb_s": r["ttfb_s"],
                         "total_s": r["total_s"], "finish_reason": r["finish_reason"],
                         "model_field": r["model_field"], "headers": r["headers"], "usage": r["usage"],
                         "http_status": r["http_status"], "content": r["content"], "tool_calls": calls_rec}
            rounds.append(round_rec)
            if r["error"]:
                error = r["error"] + (": " + r.get("error_body", "") if r.get("error_body") else "")
                break
            if not r["tool_calls"]:
                final_text = r["content"]
                messages.append({"role": "assistant", "content": r["content"]})
                break
            messages.append({"role": "assistant", "content": r["content"] or "",
                             "tool_calls": [{"id": c["id"], "type": "function",
                                             "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                                            for c in r["tool_calls"]]})
            for c in r["tool_calls"]:
                if c["args"] is None:
                    result, meta = {"error": "Invalid", "error_text": "arguments are not valid JSON"}, \
                        {"ok": False, "targets": [], "args": {}}
                else:
                    result, meta = home.execute(c["name"], c["args"])
                calls_rec.append({"name": c["name"], "args": c["args"], "ok": meta["ok"], "targets": meta["targets"],
                                  "meta_args": meta["args"], "result": result})
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": ha_fake.result_to_content(result)})
        else:
            error = "too many tool rounds"
            final_text = rounds[-1]["content"]
        turn = {"user": spec["text"], "rounds": rounds, "final_text": final_text, "error": error}
        turn["grade"] = grade.grade_turn(spec["expect"], turn, ha_fake.READ_ONLY_TOOLS)
        turn["format"] = grade.format_check(final_text)
        last = rounds[-1]
        pre = sum((r["total_s"] or 0) for r in rounds[:-1])
        turn["timing"] = {
            "ttfs_s": None if last["ttft_s"] is None else pre + last["ttft_s"],  # time to first spoken word
            "total_s": sum((r["total_s"] or 0) for r in rounds), "rounds": len(rounds)}
        turns.append(turn)
        if error:
            break
    return turns


# --- statistics ---------------------------------------------------------------

def pct(values, p):
    """Nearest-rank percentile (small samples; no interpolation)."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    return vals[max(0, math.ceil(p / 100 * len(vals)) - 1)]


def _f(v):
    return "n/a" if v is None else f"{v:.2f}"


def summarize(records):
    """records: per-conversation dicts as written to disk. Returns a dict of groups."""
    groups = {}

    def add(key, turn, rec):
        g = groups.setdefault(key, {"n": 0, "pass": 0, "fail": 0, "review": 0, "error": 0, "first_ok": 0,
                                    "first_n": 0, "fmt_ok": 0, "long": 0, "ttfs": [], "total": [], "req_ttft": [],
                                    "req_total": [], "models": {}, "backends": {}, "errors": {}})
        g["n"] += 1
        status = turn["grade"]["status"]
        g[status] += 1
        if turn["grade"]["first_round_ok"] is not None:
            g["first_n"] += 1
            g["first_ok"] += bool(turn["grade"]["first_round_ok"])
        g["fmt_ok"] += turn["format"]["ok"]
        g["long"] += turn["format"]["long"]
        if status != "error":
            g["ttfs"].append(turn["timing"]["ttfs_s"])
            g["total"].append(turn["timing"]["total_s"])
        for r in turn["rounds"]:
            g["req_ttft"].append(r["ttft_s"])
            g["req_total"].append(r["total_s"])
            if r.get("model_field"):
                g["models"][r["model_field"]] = g["models"].get(r["model_field"], 0) + 1
            for hk, hv in (r.get("headers") or {}).items():
                if "backend" in hk or "upstream" in hk or "provider" in hk or "served" in hk:
                    g["backends"][hv] = g["backends"].get(hv, 0) + 1
            if r.get("http_status") and r["http_status"] != 200:
                g["errors"][str(r["http_status"])] = g["errors"].get(str(r["http_status"]), 0) + 1
        if turn["error"] and not any(r.get("http_status") not in (None, 200) for r in turn["rounds"]):
            g["errors"]["other"] = g["errors"].get("other", 0) + 1

    for rec in records:
        for turn in rec["turns"]:
            add((rec["label"], rec["category"]), turn, rec)
            if rec["category"] == "control":
                add((rec["label"], "control/" + rec["kind"]), turn, rec)
            add((rec["label"], "ALL"), turn, rec)
    return groups


def render_table(groups):
    rows = ["| run | group | turns | pass | pass % (excl. review/error) | first-call ok % | review | error | "
            "TTFS p50 / p95 (s) | total p50 / p95 (s) | request TTFT p50 / p95 (s) | spoken format ok % |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- | ---: |"]
    for (label, cat), g in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] != "ALL", kv[0][1])):
        graded = g["pass"] + g["fail"]
        rate = f"{100 * g['pass'] / graded:.0f}" if graded else "n/a"
        first = f"{100 * g['first_ok'] / g['first_n']:.0f}" if g["first_n"] else "n/a"
        fmt = f"{100 * g['fmt_ok'] / g['n']:.0f}" if g["n"] else "n/a"
        rows.append(f"| {label} | {cat} | {g['n']} | {g['pass']} | {rate} | {first} | {g['review']} | {g['error']} | "
                    f"{_f(pct(g['ttfs'], 50))} / {_f(pct(g['ttfs'], 95))} | "
                    f"{_f(pct(g['total'], 50))} / {_f(pct(g['total'], 95))} | "
                    f"{_f(pct(g['req_ttft'], 50))} / {_f(pct(g['req_ttft'], 95))} | {fmt} |")
    return "\n".join(rows)


def render_extras(groups):
    out = []
    for (label, cat), g in sorted(groups.items()):
        if cat != "ALL":
            continue
        out.append(f"- {label}: models seen {g['models'] or 'none'}; backend headers {g['backends'] or 'none'}; "
                   f"HTTP/other errors {g['errors'] or 'none'}")
    return "\n".join(out)


# --- I/O ------------------------------------------------------------------------

def git_commit():
    try:
        return subprocess.check_output(["git", "-C", REPO, "rev-parse", "--short", "HEAD"], text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return None


def ledger_path(out_root):
    return os.path.join(out_root, "request_ledger.jsonl")


def ledger_total(out_root, host):
    total = 0
    try:
        with open(ledger_path(out_root), encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                if row.get("host") == host:
                    total += row.get("requests", 0)
    except (OSError, json.JSONDecodeError):
        pass
    return total


def load_records(dirs):
    recs = []
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "*_r*.json"))):
            with open(path, encoding="utf-8") as fh:
                recs.append(json.load(fh))
    return recs


def cmd_plan(args):
    items = load_phrases(args.phrases)
    cats = args.categories.split(",")
    jobs = plan_items(items, cats, args.repeat_control, args.sample, args.seed, set(args.ids.split(",")) if args.ids else None)
    print(f"phrases loaded: {len(items)}; jobs: {len(jobs)}; estimated requests: ~{worst_case_requests(jobs)}")
    by = {}
    for it, _ in jobs:
        by[it["category"]] = by.get(it["category"], 0) + 1
    print("jobs per category:", by)


def cmd_run(args):
    items = load_phrases(args.phrases)
    cats = args.categories.split(",")
    ids = set(args.ids.split(",")) if args.ids else None
    jobs = plan_items(items, cats, args.repeat_control, args.sample, args.seed, ids)
    est = worst_case_requests(jobs)
    host = urllib.parse.urlparse(args.base_url).netloc
    out_root = os.path.abspath(args.out)
    ledger_used = ledger_total(out_root, host)
    print(f"{len(jobs)} jobs, ~{est} requests; ledger for {host}: {ledger_used} used of {args.total_budget}")
    if args.total_budget and ledger_used + est > args.total_budget and not args.force:
        sys.exit("planned requests would exceed the total budget; lower --sample/--repeat-control or pass --force")
    if os.path.abspath(out_root).startswith(REPO + os.sep):
        sys.exit("refusing to write results inside the homelab repository; use a git-ignored directory (--out)")
    api_key = os.environ.get(args.api_key_env, "") if args.api_key_env else ""
    extra = json.loads(args.extra_body) if args.extra_body else None
    home = ha_fake.FakeHome(args.inventory)
    tools = ha_fake.build_tools()
    budget = client.RequestBudget(args.max_requests, ledger_used, args.total_budget or None)
    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(out_root, args.label, run_id)
    os.makedirs(run_dir, exist_ok=True)
    meta = {"label": args.label, "model": args.model, "host": host, "run_id": run_id, "commit": git_commit(),
            "temperature": args.temperature, "max_tokens": args.max_tokens, "extra_body": extra,
            "tools": len(tools), "system_prompt_sha256": hashlib.sha256(home.system_prompt().encode()).hexdigest()[:16],
            "repeat_control": args.repeat_control, "categories": cats, "started": time.time()}

    def send(messages):
        return client.chat_stream(args.base_url, args.model, messages, tools, api_key or None, args.timeout,
                                  args.temperature, args.max_tokens, extra, not args.no_usage)

    records = []
    try:
        if args.warmup:
            budget.take()
            w = client.chat_stream(args.base_url, args.model, [{"role": "user", "content": "Hola"}], None, api_key or None,
                                   args.timeout, args.temperature, 8, extra, False)
            print(f"warmup: {w['error'] or 'ok'} in {w['total_s']:.1f}s (not part of the statistics)")
        for n, (it, rep) in enumerate(jobs, 1):
            turns = run_conversation(it, home, tools, send, budget)
            rec = {"label": args.label, "model": args.model, "id": it["id"], "rep": rep, "category": it["category"],
                   "kind": it["kind"], "turns": turns}
            with open(os.path.join(run_dir, f"{it['id']}_r{rep}.json"), "w", encoding="utf-8") as fh:
                json.dump(rec, fh, ensure_ascii=False, indent=1)
            records.append(rec)
            g = turns[-1]["grade"]["status"]
            print(f"[{n}/{len(jobs)}] {it['id']} r{rep}: {g} (req so far {budget.count})", flush=True)
            time.sleep(args.pause)
    except client.BudgetExceeded as exc:
        print("STOPPED:", exc, file=sys.stderr)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
    finally:
        meta["requests"] = budget.count
        meta["finished"] = time.time()
        with open(os.path.join(run_dir, "run_meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=1)
        os.makedirs(out_root, exist_ok=True)
        with open(ledger_path(out_root), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), "host": host, "label": args.label, "requests": budget.count}) + "\n")
    groups = summarize(records)
    text = render_table(groups) + "\n\n" + render_extras(groups)
    with open(os.path.join(run_dir, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(text)
    print("results:", run_dir)


def cmd_summarize(args):
    groups = summarize(load_records(args.dirs))
    print(render_table(groups))
    print()
    print(render_extras(groups))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--phrases", default=None)
        p.add_argument("--categories", default=",".join(CATEGORIES))
        p.add_argument("--repeat-control", type=int, default=2, help="repetitions of control phrases (default 2)")
        p.add_argument("--sample", type=int, default=0, help="random sample of N phrases (e.g. 10 for a small tier check)")
        p.add_argument("--seed", type=int, default=1)
        p.add_argument("--ids", default="", help="comma-separated phrase ids")

    p = sub.add_parser("plan", help="count jobs and estimated requests; no network")
    common(p)
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("run", help="run against an OpenAI-compatible endpoint")
    common(p)
    p.add_argument("--base-url", required=True, help="e.g. https://host/v1 or http://ollama:11434/v1")
    p.add_argument("--model", required=True)
    p.add_argument("--label", required=True, help="name of this run, e.g. kiais-fast")
    p.add_argument("--api-key-env", default="AGENT_EVAL_API_KEY", help="env var holding the key (never written to results)")
    p.add_argument("--inventory", default=None)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--temperature", type=float, default=0.2)
    p.add_argument("--max-tokens", type=int, default=400)
    p.add_argument("--timeout", type=float, default=90.0)
    p.add_argument("--pause", type=float, default=0.5, help="seconds between conversations")
    p.add_argument("--extra-body", default="", help="JSON merged into the request body (e.g. Ollama options)")
    p.add_argument("--no-usage", action="store_true", help="do not send stream_options.include_usage")
    p.add_argument("--warmup", action="store_true", help="one tiny request first (loads a local model); excluded from stats")
    p.add_argument("--max-requests", type=int, default=300, help="hard cap for this run")
    p.add_argument("--total-budget", type=int, default=600, help="cap across runs per host (ledger); 0 disables")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("summarize", help="summary table from result directories")
    p.add_argument("dirs", nargs="+")
    p.set_defaults(fn=cmd_summarize)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
