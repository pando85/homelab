#!/usr/bin/env python3
"""Headroom of a decider behind Home Assistant's built-in intents, before building one.

On the existing Whisper transcripts of the small-models corpus (stt_eval.py run with the static
verbs-only prompt; 5 intelligible Piper voices x clean / +10 / +5 / 0 dB TV content), take every
control command the built-in intents miss (HA would hand it to the conversation agent) and put it
in one category by documented rules:

  a  misheard: Whisper lost a key word (a content word of the reference has no exact or fuzzy
     match in the transcript, or the spoken number is gone); intent or target cannot be
     recovered from the text alone.
  a-verb  as a, but the only lost word is the leading verb ("Con un temporizador de 10 minutos");
     target and value survive, so the intent is sometimes inferable (upper bound only).
  b1 phrasing: every key word survives (exact or fuzzy) and the REFERENCE text itself is not
     handled by the built-in sentences: a decider (or a custom sentence) could rescue it.
  b2 near-miss: every key word survives but at least one only fuzzily (e.g. "pesiana"), and the
     reference IS handled: strict matching fails on a small mishearing a robust decider could absorb.
  c  other: every key word survives exactly and the reference is handled, yet the transcript
     misses (number or punctuation format, extra words, area ambiguity).

Key words: the reference's words after the Phase 2 normalisation (numbers spelled), minus a
stop-word list and fillers ("kivi", "por favor"). Fuzzy match: rapidfuzz ratio >= 80 against any
transcript word (or two-word join). Number check: numbers_es.parse_number on both sides.

Commands: control phrases of agent_eval/phrases_es.yaml whose expectation is a tool call
(state questions c35-c37, the review item c43 and the missing-target c44 are not commands).

  python headroom.py [--run turbo_b5_vonly] [--samples 4]
Needs hassil==3.12.1, home-assistant-intents==2026.10.6, pyyaml, rapidfuzz and the transcripts in
$KIVI_PRIVATE/stt-runs (private, not in this repository).
"""
import argparse
import collections
import csv
import os
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "small_models"))
sys.path.insert(0, str(HERE.parent / "agent_eval"))
import numbers_es as ne  # noqa: E402
import stt_eval  # noqa: E402  (RUNS, EXCLUDED_VOICES, p2 normalisation via sys.path)

STOP = set("""a al de del el la las los lo le un una unos unas y o en con por para que me te se mi tu
su es esta este esto ya muy mas aqui kivi favor porfa oye""".split())
CONDS = ["clean", "snr+10", "snr+5", "snr+0"]
CATS = ["a", "a-verb", "b1", "b2", "c"]


def norm_words(text):
    import p2_score
    w, _ = p2_score.normalize((text or "").replace("%", " por ciento "), spell_numbers=True)
    return p2_score._v11(w)


def key_words(ref):
    return [w for w in norm_words(ref) if w not in STOP]


def keyword_status(ref, hyp, threshold=80):
    """('exact' | 'fuzzy' | 'lost', detail). Lost if any key word has no match >= threshold or
    the reference's number is not parsed from the transcript."""
    from rapidfuzz import fuzz
    hw = norm_words(hyp)
    cands = set(hw) | {a + b for a, b in zip(hw, hw[1:])}
    worst, detail = "exact", []
    for k in key_words(ref):
        if k in hw:
            continue
        best = max((fuzz.ratio(k, c) for c in cands), default=0)
        if best >= threshold:
            worst = "fuzzy" if worst == "exact" else worst
            detail.append(f"~{k}")
        else:
            worst = "lost"
            detail.append(f"-{k}")
    rn, hn = ne.parse_number(ref), ne.parse_number(hyp)
    if rn is not None and rn != hn:
        worst = "lost"
        detail.append(f"#{rn}->{hn}")
    if worst == "lost":
        verb = norm_words(ref)[0]
        lost = [d for d in detail if d[0] in "-#"]
        if lost == [f"-{verb}"]:
            worst = "lost-verb"
    return worst, " ".join(detail)


def category(ref_handled, kw):
    if kw == "lost-verb":
        return "a-verb"
    if kw == "lost":
        return "a"
    if not ref_handled:
        return "b1"
    return "b2" if kw == "fuzzy" else "c"


def builtin_outcome_fn(custom=None):
    """f(phrase_id, text) -> 'pass' | 'wrong' | 'local' | 'miss', following builtin_intents.py:
    no match, a match HA ignores for an LLM agent, or an unresolved target falls through (miss)."""
    import builtin_intents as bi
    import grade
    import ha_fake
    import run as ae
    home = ha_fake.FakeHome()
    if custom:  # custom sentences first, then the built-in ones (headroom_v1.build_matcher)
        import headroom_v1
        match = headroom_v1.build_matcher(home, custom)
    else:
        match = bi.build_matcher(home)
    tools = [t["function"]["name"] for t in ha_fake.build_tools()]
    expect = {i["id"]: i["turns"][0]["expect"] for i in ae.load_phrases()}
    cache = {}

    def f(pid, text):
        key = (pid, text)
        if key in cache:
            return cache[key]
        res = match(bi.remove_punctuation(text))
        if res is None:
            out = "miss"
        else:
            intent, tool, slots = bi.to_call(res, home, tools)
            if intent in bi.LLM_IGNORED:
                out = "miss"
            elif tool is None:
                out = "local"
            else:
                _r, meta = home.execute(tool, slots)
                if not meta["ok"]:
                    out = "miss"
                else:
                    call = {"name": tool, "args": slots, "ok": True, "targets": meta["targets"],
                            "meta_args": meta["args"]}
                    g = grade.grade_turn(bi._strip_answer(expect[pid]), {"rounds": [{"tool_calls": [call]}],
                                                                         "final_text": "?"}, ha_fake.READ_ONLY_TOOLS)
                    out = "pass" if g["status"] == "pass" else "wrong"
        cache[key] = out
        return out
    return f


def is_command(expect):
    alts = expect.get("any_of") or [expect]
    return any(a.get("tool") for a in alts)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default="turbo_b5_vonly")
    ap.add_argument("--samples", type=int, default=4)
    ap.add_argument("--seconds", type=float, default=3.0, help="saving per rescued command")
    ap.add_argument("--dump", default=None, help="write the per-item table to this CSV (private)")
    ap.add_argument("--custom", default=None, help="custom sentences file matched before the built-in ones")
    a = ap.parse_args()
    import run as ae
    phrases = {i["id"]: i for i in ae.load_phrases() if i["category"] == "control"}
    cmds = {pid for pid, i in phrases.items() if is_command(i["turns"][0]["expect"])}
    outcome = builtin_outcome_fn(a.custom)
    rows = [r for r in csv.DictReader(open(stt_eval.RUNS / f"{a.run}.csv"))
            if r["voice"] not in stt_eval.EXCLUDED_VOICES and r["id"] in cmds]
    ref_ok = {pid: outcome(pid, phrases[pid]["turns"][0]["text"]) for pid in cmds}
    table = []
    for r in rows:
        o = outcome(r["id"], r["hyp"])
        cat = kw = detail = ""
        if o == "miss":
            kw, detail = keyword_status(r["text"], r["hyp"])
            cat = category(ref_ok[r["id"]] == "pass", kw)
        table.append(dict(r, outcome=o, ref=ref_ok[r["id"]], kw=kw, detail=detail, cat=cat))
    print(f"Commands: {len(cmds)} phrases; reference texts handled by the built-in intents: "
          f"{sum(v == 'pass' for v in ref_ok.values())} (wrong {sum(v == 'wrong' for v in ref_ok.values())}, "
          f"miss {sum(v == 'miss' for v in ref_ok.values())}, local {sum(v == 'local' for v in ref_ok.values())})")
    print("Reference misses:", ", ".join(sorted(p for p, v in ref_ok.items() if v == "miss")))
    print()
    print("| cond | commands | built-in correct | built-in wrong | miss | a misheard | b1 phrasing | b2 near-miss | "
          "c other | a-verb only verb lost | b1+b2 x 3 s per command |")
    print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    tot = collections.Counter()
    for cond in CONDS + ["all"]:
        t = [x for x in table if cond == "all" or x["cond"] == cond]
        n = len(t)
        c = collections.Counter(x["outcome"] for x in t)
        k = collections.Counter(x["cat"] for x in t if x["cat"])
        pc = lambda v: f"{v} ({v / n:.0%})"  # noqa: E731
        save = (k["b1"] + k["b2"]) * a.seconds / n
        print(f"| {cond} | {n} | {pc(c['pass'])} | {pc(c['wrong'])} | {pc(c['miss'])} | {pc(k['a'])} | "
              f"{pc(k['b1'])} | {pc(k['b2'])} | {pc(k['c'])} | {pc(k['a-verb'])} | {save:.2f} s |")
        if cond == "all":
            tot = k
    print()
    print("b1 by phrase (count over all conditions):")
    by = collections.Counter(x["id"] for x in table if x["cat"] == "b1")
    for pid, v in by.most_common():
        print(f"  {pid} {v:3d}  {phrases[pid]['turns'][0]['text']}")
    print("a by phrase:")
    by = collections.Counter(x["id"] for x in table if x["cat"] == "a")
    for pid, v in by.most_common():
        print(f"  {pid} {v:3d}  {phrases[pid]['turns'][0]['text']}")
    rng = random.Random(5)
    for cat in CATS:
        xs = [x for x in table if x["cat"] == cat]
        print(f"\nSample {cat} ({len(xs)}):")
        for x in rng.sample(xs, min(a.samples, len(xs))):
            print(f"  {x['id']} {x['cond']:7} {x['text']!r} -> {x['hyp']!r} [{x['detail']}]")
    if a.dump:
        with open(a.dump, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(table[0]))
            w.writeheader()
            w.writerows(table)
    del tot


if __name__ == "__main__":
    main()
