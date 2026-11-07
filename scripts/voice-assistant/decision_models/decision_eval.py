#!/usr/bin/env python3
"""Offline evaluation of a decision stage behind Home Assistant's built-in intents (Kivi).

Test items: Whisper transcripts of the synthetic corpus (small_models/stt_eval.py runs; default the
verbs-only prompt run turbo_b5_vonly; clean, +5 and 0 dB of TV content; the two unintelligible mls
voices excluded), or a templated phrase set (real_phrases.py, text only). Each transcript first goes
through the built-in matcher (agent_eval/builtin_intents.py, hassil + home-assistant-intents as
pinned for HA 2026.10.4); only what it does not answer reaches the decision stage.

  run      decide every item with each candidate, cache the results (JSONL, private directory)
  report   threshold sweep, chosen threshold, per-noise tables, paired McNemar against the baseline
  bench    latency (CPU threads, GPU fp32) and peak VRAM, one candidate per process

  python decision_eval.py run --candidates baseline laya-ml quyet-tiny --langs es en
  python decision_eval.py report
  python decision_eval.py bench --candidate quyet-tiny --device cuda

Outcome of an item that reaches the stage: rescued (the call matches the phrase's expectation:
intent, exact targets, value), wrong (an executed call that does not, or any call on a general,
live or state question or an unknown target), handoff (to the LLM agent).
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT_EVAL = Path(os.environ.get("AGENT_EVAL_DIR", HERE.parent / "agent_eval"))
WEBOS = Path(os.environ.get("WEBOS_REPO", Path.home() / "webos-voice-satellite"))
PRIVATE = Path(os.environ.get("KIVI_PRIVATE", WEBOS / "devices" / "private" / "kivi"))
OUT = Path(os.environ.get("DECISION_OUT", PRIVATE / "decision-models"))
MODELS = Path(os.environ.get("DECISION_MODELS", "/work/workspaces/kivi-decision/hf/hub"))
sys.path.insert(0, str(AGENT_EVAL))
sys.path.insert(0, str(HERE))

import decide as D  # noqa: E402

EXCLUDED_VOICES = {"mls9972", "mls10246"}
CONDS = ("clean", "snr+5", "snr+0")
PATHS = {
    "laya": MODELS / "models--convaiinnovations--laya/snapshots/7b928d828b7b0e022f929d9bd2e44165aa270148",
    "quyet-tiny": MODELS / "models--chinhnc--Quyet-1.0-Tiny/snapshots/fba8ca9292a705e21b14a4c5150e79a4ddcc8db3",
    "quyet-small": MODELS / "models--chinhnc--Quyet-1.0-Small/snapshots/f007dfeb4874f0f3b3a3cc0a63f4d4192164c1c2",
    "granite": MODELS / ("models--ibm-granite--granite-embedding-278m-multilingual/snapshots/"
                         "a9cb5338491faf32b73dd17b714a31821c021bbf"),
}
THRESHOLDS = [round(x / 100, 2) for x in range(0, 100, 5)] + [0.97, 0.98, 0.99, 0.995, 0.999]


def strip_expect(exp):
    keep = lambda alt: {k: v for k, v in alt.items()
                        if not k.startswith(("answer", "must_not", "limitation", "no_success", "tools_any"))}
    return {"any_of": [keep(a) for a in exp["any_of"]]} if "any_of" in exp else keep(exp)


def gold_kind(category, expect):
    """'act' (a call can be graded), 'noact' (any call is wrong) or None (not graded: review)."""
    if category != "control":
        return "noact"
    alts = expect.get("any_of", [expect])
    if all(a.get("review") for a in alts):
        return None
    return "act" if any(a.get("tool") or a.get("calls") for a in alts) else "noact"


def make_grader(expect):
    import grade
    import ha_fake
    exp = strip_expect(expect)

    def ok(call):
        g = grade.grade_turn(exp, {"rounds": [{"tool_calls": [call]}], "final_text": ""}, ha_fake.READ_ONLY_TOOLS)
        return g["status"] == "pass"
    return ok


def builtin_checker(inventory_path=None):
    """f(text, expect) -> 'local-pass' | 'local-wrong' | 'local-other' | 'fallthrough' (HA hands it on)."""
    import builtin_intents as bi
    import ha_fake
    home = ha_fake.FakeHome(inventory_path)
    match = bi.build_matcher(home)
    tool_names = [t["function"]["name"] for t in ha_fake.build_tools()]

    def check(text, expect, gk):
        res = match(bi.remove_punctuation(text))
        if res is None or res.intent.name in bi.LLM_IGNORED:
            return "fallthrough"
        _intent, tool, slots = bi.to_call(res, home, tool_names)
        if tool is None:
            return "local-other"
        _r, meta = home.execute(tool, slots)
        if not meta["ok"]:
            return "fallthrough"
        call = {"name": tool, "args": slots, "ok": True, "targets": meta["targets"], "meta_args": meta["args"]}
        if gk != "act":
            return "local-wrong" if tool not in ha_fake.READ_ONLY_TOOLS else "local-other"
        return "local-pass" if make_grader(expect)(call) else "local-wrong"
    return check, home


def load_phrases():
    import run as ae
    return {it["id"]: {"category": it["category"], "kind": it["turns"][0].get("kind") or it.get("kind"),
                       "text": it["turns"][0]["text"], "expect": it["turns"][0]["expect"]}
            for it in ae.load_phrases() if it["category"] != "followup"}


def stt_items(run_name, conds=CONDS):
    ph = load_phrases()
    items = []
    for r in csv.DictReader(open(PRIVATE / "stt-runs" / f"{run_name}.csv")):
        if r["voice"] in EXCLUDED_VOICES or r["cond"] not in conds or r["id"] not in ph:
            continue
        p = ph[r["id"]]
        gk = gold_kind(p["category"], p["expect"])
        if gk is None:
            continue
        items.append({"key": f"{r['id']}|{r['voice']}|{r['cond']}", "id": r["id"], "voice": r["voice"],
                      "cond": r["cond"], "category": p["category"], "text": r["hyp"], "ref": p["text"],
                      "expect": p["expect"], "gold": gk})
    return items


def phrase_set_items(path):
    """Templated items (real_phrases.py output, JSONL) as text-only items, cond 'text'."""
    items = []
    for line in open(path, encoding="utf-8"):
        p = json.loads(line)
        items.append({"key": p["id"] + "|text|text", "id": p["id"], "voice": "text", "cond": "text",
                      "category": p["category"], "text": p["text"], "ref": p["text"], "expect": p["expect"],
                      "gold": p["gold"], "template": p.get("template")})
    return items


# kivi-fake-v1 (decider_poc) intents -> the expectation language of agent_eval/grade.py
FAKE_V1_TOOLS = {
    "turn_on": [{"tool": "intent__HassTurnOn"}], "turn_off": [{"tool": "intent__HassTurnOff"}],
    "open_cover": [{"tool": "intent__HassTurnOn"}, {"tool": "intent__HassSetPosition", "args": {"position": 100}}],
    "close_cover": [{"tool": "intent__HassTurnOff"}, {"tool": "intent__HassSetPosition", "args": {"position": 0}}],
    "cover_position": [{"tool": "intent__HassSetPosition", "value": "position"}],
    "stop_cover": [{"tool": "intent__HassStopMoving"}],
    "light_set": [{"tool": "light__HassLightSet", "value": "brightness"}],
    "set_volume": [{"tool": "media_player__HassSetVolume", "value": "volume_level"}],
    "volume_up": [{"tool": "media_player__HassSetVolumeRelative", "args": {"volume_dir": "up"}}],
    "volume_down": [{"tool": "media_player__HassSetVolumeRelative", "args": {"volume_dir": "down"}}],
    "mute": [{"tool": "media_player__HassMediaPlayerMute"}], "unmute": [{"tool": "media_player__HassMediaPlayerUnmute"}],
    "pause": [{"tool": "media_player__HassMediaPause"}], "resume": [{"tool": "media_player__HassMediaUnpause"}],
    "next": [{"tool": "media_player__HassMediaNext"}], "previous": [{"tool": "media_player__HassMediaPrevious"}],
    "climate_set": [{"tool": "climate__HassClimateSetTemperature", "value": "temperature"}],
    "fan_speed": [{"tool": "fan__HassFanSetSpeed", "value": "percentage"}],
    "start_timer": [{"tool": "intent__HassStartTimer", "value": "total_seconds", "notarget": True}],
    "cancel_timer": [{"tool": ["intent__HassCancelTimer", "intent__HassCancelAllTimers"], "notarget": True}],
}


def fake_v1_expect(it):
    """Gold expectation of a kivi-fake-v1 item; ("noact", {}) for everything that is not a single command."""
    if it["kind"] != "command" or len(it["actions"]) != 1:
        return "noact", {"no_success_claim": True}
    act, ents = it["actions"][0], it["entities"][0] if it["entities"] else []
    alts = []
    for spec in FAKE_V1_TOOLS[act["intent"]]:
        alt = {"tool": spec["tool"]}
        if not spec.get("notarget"):
            alt["targets"] = list(ents)
        args = dict(spec.get("args", {}))
        if spec.get("value"):
            args[spec["value"]] = act["value"]
        if args:
            alt["args"] = args
        alts.append(alt)
    return "act", (alts[0] if len(alts) == 1 else {"any_of": alts})


def fake_v1_items(root, files=("items.jsonl",), stt=None):
    """kivi-fake-v1 items (read-only). Text items get cond 'text'; with stt=<csv>, the audio subset's
    Whisper transcripts replace the text (one item per row, cond from the csv)."""
    root = Path(root)
    base = {}
    for f in files:
        p = root / f
        if p.exists():
            for line in open(p, encoding="utf-8"):
                it = json.loads(line)
                base[it["id"]] = it
    rows = []
    if stt:
        for r in csv.DictReader(open(stt, encoding="utf-8")):
            it = base.get(r["id"])
            if it is None:
                continue
            rows.append((it, r["hyp"], r.get("voice", "?"), r.get("cond", "?")))
    else:
        rows = [(it, it["text"], "text", "text") for it in base.values()]
    items = []
    for it, text, voice, cond in rows:
        gold, exp = fake_v1_expect(it)
        items.append({"key": f"{it['id']}|{voice}|{cond}", "id": it["id"], "voice": voice, "cond": cond,
                      "category": "control" if it["kind"] == "command" else it["kind"], "text": text,
                      "ref": it["text"], "expect": exp, "gold": gold, "style": it["style"],
                      "source": it.get("source"), "home": it["home"], "inv": str(root / "homes" / f"{it['home']}.yaml")})
    return items


def cmd_run(a):
    import yaml
    inv_default = str(a.inventory or AGENT_EVAL / "inventory_es.yaml")
    if a.fake_v1:
        items = fake_v1_items(a.fake_v1, stt=a.fake_v1_stt)
    else:
        items = phrase_set_items(a.phrases) if a.phrases else stt_items(a.stt)
    homes = {}
    for it in items:
        inv_path = it.get("inv", inv_default)
        if inv_path not in homes:
            homes[inv_path] = (D.Home(yaml.safe_load(open(inv_path, encoding="utf-8"))), builtin_checker(inv_path)[0])
        it["builtin"] = homes[inv_path][1](it["text"], it["expect"], it["gold"])
    OUT.mkdir(parents=True, exist_ok=True)
    os.chmod(OUT, 0o700)
    import torch
    torch.set_num_threads(a.threads)
    import backends
    for cand in a.candidates:
        be = backends.load(cand, a.device, True, {k: str(v) for k, v in PATHS.items()})
        for lang in (["es"] if cand == "baseline" else a.langs):
            # a trailing "N" (esN, enN) sends neutral option keys A, B, ... (backends.neutral_keys)
            ask = backends.neutral_keys(be.ask) if lang.endswith("N") else be.ask
            out = OUT / f"{a.tag}__{cand}__{lang}.jsonl"
            cache = {}
            t_all = time.perf_counter()
            with open(out, "w", encoding="utf-8") as fh:
                for it in items:
                    home = homes[it.get("inv", inv_default)][0]
                    ck = (it.get("inv", inv_default), it["text"])
                    if ck not in cache:
                        t0 = time.perf_counter()
                        r = D.decide(ask, home, it["text"], lang.rstrip("N"), be.max_options)
                        r["ms"] = (time.perf_counter() - t0) * 1000
                        cache[ck] = r
                    r = cache[ck]
                    grader = make_grader(it["expect"]) if it["gold"] == "act" else None
                    full = D.outcome(r["call"], it["gold"], grader) if r["call"] else "handoff"
                    rec = {k: it[k] for k in ("key", "id", "voice", "cond", "category", "gold", "builtin")}
                    rec.update(gate=r["gate"], outcome_ungated=full, decision=r["decision"],
                               confidence=r["confidence"], reason=r["reason"], calls=r["calls"], ms=round(r["ms"], 1),
                               call=r["call"] and {k: r["call"][k] for k in ("name", "targets", "meta_args")})
                    for k in ("template", "style", "source", "home"):
                        if it.get(k):
                            rec[k] = it[k]
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"{cand} {lang}: {len(items)} items, {len(cache)} distinct texts, "
                  f"{time.perf_counter() - t_all:.0f} s -> {out.name}", file=sys.stderr, flush=True)
        del be


# ---- report ----

def load_run(tag, cand, lang):
    p = OUT / f"{tag}__{cand}__{lang}.jsonl"
    return [json.loads(x) for x in open(p, encoding="utf-8")] if p.exists() else None


def at(rec, thr):
    if rec["outcome_ungated"] == "handoff" or rec["gate"] < thr:
        return "handoff"
    return rec["outcome_ungated"]


def subsets(recs):
    """missed: home commands the built-in intents did not answer ('act' items that fall through);
    other: general, live and state questions that reach the stage (any action is wrong);
    matched: commands the built-in intents answered correctly (sanity: stage run as if first)."""
    return {"missed": [r for r in recs if r["gold"] == "act" and r["builtin"] == "fallthrough"],
            "other": [r for r in recs if r["gold"] == "noact" and r["builtin"] == "fallthrough"],
            "matched": [r for r in recs if r["gold"] == "act" and r["builtin"] == "local-pass"]}


def counts(recs, thr):
    c = {"rescued": 0, "wrong": 0, "handoff": 0}
    for r in recs:
        c[at(r, thr)] += 1
    return c


def choose_threshold(recs, max_wrong_rate=0.01):
    """Lowest threshold whose wrong actions over everything that reaches the stage (missed + other)
    are at most max_wrong_rate (in-sample)."""
    s = subsets(recs)
    stage = s["missed"] + s["other"]
    for thr in THRESHOLDS:
        if counts(stage, thr)["wrong"] <= max_wrong_rate * len(stage):
            return thr
    return 1.0


def pct(k, n):
    return f"{k}/{n} ({100 * k / n:.0f} %)" if n else "0/0"


def cmd_report(a):
    rows = []
    runs = {}
    for cand in a.candidates:
        for lang in (["es"] if cand == "baseline" else a.langs):
            recs = load_run(a.tag, cand, lang)
            if recs:
                runs[(cand, lang)] = recs
    if not runs:
        raise SystemExit("no runs")
    any_recs = next(iter(runs.values()))
    conds = sorted({r["cond"] for r in any_recs}, key=lambda c: (c != "clean", c != "text", c))
    s0 = subsets(any_recs)
    print(f"## Test set ({a.tag})\n")
    for c in conds:
        rc = [r for r in any_recs if r["cond"] == c]
        s = subsets(rc)
        bl = {k: sum(r["builtin"] == k for r in rc if r["gold"] == "act") for k in
              ("local-pass", "local-wrong", "local-other", "fallthrough")}
        print(f"- {c}: graded commands {sum(r['gold'] == 'act' for r in rc)} (built-in: {bl}); reach the stage: "
              f"missed commands {len(s['missed'])}, general/live/state {len(s['other'])}")
    print(f"- total missed {len(s0['missed'])}, other {len(s0['other'])}, matched (sanity) {len(s0['matched'])}\n")

    print("## Threshold sweep (pooled conditions): missed commands rescued / wrong; other: wrong\n")
    print("| candidate | lang | " + " | ".join(str(t) for t in a.sweep) + " |")
    print("| --- | --- | " + " | ".join("---" for _ in a.sweep) + " |")
    for (cand, lang), recs in runs.items():
        s = subsets(recs)
        cells = []
        for t in a.sweep:
            m, o = counts(s["missed"], t), counts(s["other"], t)
            cells.append(f"{m['rescued']}/{m['wrong']}; {o['wrong']}")
        print(f"| {cand} | {lang} | " + " | ".join(cells) + " |")

    print("\n## At the chosen threshold (lowest with wrong actions <= 1 % of stage inputs, in-sample)\n")
    print("| candidate | lang | thr | cond | missed: rescued | missed: wrong | missed: handoff | other: wrong | "
          "matched (sanity): correct / wrong | p50 / p95 ms (run) |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    chosen = {}
    for (cand, lang), recs in runs.items():
        thr = a.threshold if a.threshold is not None else choose_threshold(recs)
        chosen[(cand, lang)] = thr
        for c in conds + ["all"]:
            rc = recs if c == "all" else [r for r in recs if r["cond"] == c]
            s = subsets(rc)
            m, o, ma = counts(s["missed"], thr), counts(s["other"], thr), counts(s["matched"], thr)
            n = len(s["missed"])
            ms = sorted(r["ms"] for r in rc)
            lat = f"{ms[len(ms) // 2]:.0f} / {ms[int(len(ms) * 0.95)]:.0f}" if ms else ""
            print(f"| {cand} | {lang} | {thr} | {c} | {pct(m['rescued'], n)} | {pct(m['wrong'], n)} | "
                  f"{pct(m['handoff'], n)} | {pct(o['wrong'], len(s['other']))} | "
                  f"{ma['rescued']} / {ma['wrong']} of {len(s['matched'])} | {lat} |")

    # held-out check: threshold chosen on even-numbered phrases, applied to odd ones, and the reverse
    print("\n## Phrase-split check (threshold chosen on one half of the phrases, applied to the other)\n")
    print("| candidate | lang | thr even->odd | odd: rescued / wrong / n | thr odd->even | even: rescued / wrong / n |")
    print("| --- | --- | --- | --- | --- | --- |")
    for (cand, lang), recs in runs.items():
        half = lambda r, k: int(re.sub(r"\D", "", r["id"]) or 0) % 2 == k
        cells = []
        for k in (0, 1):
            fit = [r for r in recs if half(r, k)]
            test = [r for r in recs if not half(r, k)]
            thr = choose_threshold(fit)
            s = subsets(test)
            m, o = counts(s["missed"], thr), counts(s["other"], thr)
            cells += [str(thr), f"{m['rescued']} / {m['wrong'] + o['wrong']} / {len(s['missed']) + len(s['other'])}"]
        print(f"| {cand} | {lang} | " + " | ".join(cells) + " |")

    # paired against the baseline at each one's chosen threshold
    if ("baseline", "es") in runs:
        print("\n## Paired against the baseline (each at its chosen threshold; exact McNemar, items not independent)\n")
        print("| candidate | lang | rescued: cand only / baseline only / p | wrong: cand only / baseline only / p |")
        print("| --- | --- | --- | --- |")
        b = {r["key"]: r for r in runs[("baseline", "es")]}
        tb = chosen[("baseline", "es")]
        for (cand, lang), recs in runs.items():
            if cand == "baseline":
                continue
            t = chosen[(cand, lang)]
            st = subsets(recs)
            keys = {r["key"] for r in st["missed"] + st["other"]}
            co = bo = cw = bw = 0
            for r in recs:
                if r["key"] not in keys:
                    continue
                x, y = at(r, t), at(b[r["key"]], tb)
                co += x == "rescued" and y != "rescued"
                bo += y == "rescued" and x != "rescued"
                cw += x == "wrong" and y != "wrong"
                bw += y == "wrong" and x != "wrong"
            print(f"| {cand} | {lang} | {co} / {bo} / {D.mcnemar_p(co, bo):.3g} | {cw} / {bw} / {D.mcnemar_p(cw, bw):.3g} |")

    if a.by:
        for (cand, lang), recs in runs.items():
            thr = chosen[(cand, lang)]
            print(f"\n## By {a.by}: {cand} {lang} at {thr} (items that reach the stage)\n")
            print(f"| {a.by} | commands n | rescued | wrong | handoff | other n | other: wrong |")
            print("| --- | --- | --- | --- | --- | --- | --- |")
            s = subsets(recs)
            groups = sorted({r.get(a.by, "?") for r in s["missed"] + s["other"]})
            for g in groups:
                m = [r for r in s["missed"] if r.get(a.by, "?") == g]
                o = [r for r in s["other"] if r.get(a.by, "?") == g]
                cm, co = counts(m, thr), counts(o, thr)
                print(f"| {g} | {len(m)} | {pct(cm['rescued'], len(m))} | {pct(cm['wrong'], len(m))} | "
                      f"{pct(cm['handoff'], len(m))} | {len(o)} | {pct(co['wrong'], len(o))} |")

    if a.details:
        for (cand, lang), recs in runs.items():
            thr = chosen[(cand, lang)]
            print(f"\n### Details {cand} {lang} thr {thr}: wrong actions and rescues per phrase\n")
            per = {}
            for r in subsets(recs)["missed"] + subsets(recs)["other"]:
                per.setdefault(r["id"], {"rescued": 0, "wrong": 0, "handoff": 0})[at(r, thr)] += 1
            for pid, c in sorted(per.items()):
                print(f"- {pid}: {c}")


# ---- bench ----

def cmd_bench(a):
    import statistics
    import torch
    import yaml
    torch.set_num_threads(a.threads)
    import backends
    inv = yaml.safe_load(open(a.inventory or AGENT_EVAL / "inventory_es.yaml", encoding="utf-8"))
    home = D.Home(inv)
    items = stt_items(a.stt, ("clean",))
    texts = list(dict.fromkeys(it["text"] for it in items))[: a.n]
    dev = a.device
    if dev.startswith("cuda"):
        torch.cuda.reset_peak_memory_stats()
    t0 = time.perf_counter()
    be = backends.load(a.candidate, dev, not a.amp, {k: str(v) for k, v in PATHS.items()})
    load_s = time.perf_counter() - t0
    params = sum(p.numel() for p in be.model.parameters())
    for t in texts[:3]:
        D.decide(be.ask, home, t, a.lang, be.max_options)    # warm-up
    times, calls = [], []
    for _ in range(a.repeat):
        for t in texts:
            if dev.startswith("cuda"):
                torch.cuda.synchronize()
            t0 = time.perf_counter()
            r = D.decide(be.ask, home, t, a.lang, be.max_options)
            if dev.startswith("cuda"):
                torch.cuda.synchronize()
            times.append((time.perf_counter() - t0) * 1000)
            calls.append(r["calls"])
    times.sort()
    res = {"candidate": a.candidate, "device": dev, "threads": a.threads, "amp": a.amp, "params_m": round(params / 1e6, 1),
           "load_s": round(load_s, 1), "n": len(times), "p50_ms": round(times[len(times) // 2], 1),
           "p95_ms": round(times[int(len(times) * 0.95)], 1), "mean_calls": round(statistics.mean(calls), 2)}
    if dev.startswith("cuda"):
        res["torch_peak_alloc_mib"] = round(torch.cuda.max_memory_allocated() / 2 ** 20)
        res["torch_peak_reserved_mib"] = round(torch.cuda.max_memory_reserved() / 2 ** 20)
        try:
            import subprocess
            q = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=10).stdout
            mine = [int(x.split(",")[1]) for x in q.strip().splitlines() if x.split(",")[0].strip() == str(os.getpid())]
            res["nvidia_smi_process_mib"] = mine[0] if mine else None
        except Exception as exc:  # noqa: BLE001
            res["nvidia_smi_process_mib"] = f"n/a ({exc.__class__.__name__})"
    import resource
    res["max_rss_mib"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
    print(json.dumps(res))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--candidates", nargs="+", default=["baseline", "laya-ml", "quyet-tiny"])
    r.add_argument("--langs", nargs="+", default=["es", "en"])
    r.add_argument("--stt", default="turbo_b5_vonly")
    r.add_argument("--phrases", default=None, help="JSONL from real_phrases.py instead of the STT run")
    r.add_argument("--inventory", default=None)
    r.add_argument("--fake-v1", default=None, help="kivi-fake-v1 directory (read-only)")
    r.add_argument("--fake-v1-stt", default=None, help="Whisper transcripts CSV of its audio subset")
    r.add_argument("--tag", default="synthetic")
    r.add_argument("--device", default="cuda")
    r.add_argument("--threads", type=int, default=16)
    p = sub.add_parser("report")
    p.add_argument("--candidates", nargs="+", default=["baseline", "laya-ml", "laya-en", "quyet-tiny", "quyet-small"])
    p.add_argument("--langs", nargs="+", default=["es", "en"])
    p.add_argument("--tag", default="synthetic")
    p.add_argument("--threshold", type=float, default=None)
    p.add_argument("--sweep", nargs="+", type=float, default=[0.0, 0.5, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99])
    p.add_argument("--details", action="store_true")
    p.add_argument("--by", default=None, help="extra table grouped by a record field (style, home, source, cond)")
    b = sub.add_parser("bench")
    b.add_argument("--candidate", required=True)
    b.add_argument("--device", default="cpu")
    b.add_argument("--threads", type=int, default=16)
    b.add_argument("--lang", default="es")
    b.add_argument("--amp", action="store_true", help="keep the package's CUDA autocast (fp16/bf16)")
    b.add_argument("--stt", default="turbo_b5_vonly")
    b.add_argument("--inventory", default=None)
    b.add_argument("--n", type=int, default=60)
    b.add_argument("--repeat", type=int, default=2)
    a = ap.parse_args()
    {"run": cmd_run, "report": cmd_report, "bench": cmd_bench}[a.cmd](a)


if __name__ == "__main__":
    main()
