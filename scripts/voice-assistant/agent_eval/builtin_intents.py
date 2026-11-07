#!/usr/bin/env python3
"""Which phrases would Home Assistant's built-in Spanish intents (the pipeline
option prefer_local_intents) handle, against the FAKE inventory?

Offline and read-only: it runs hassil with the Spanish sentences shipped in the
home-assistant-intents package, the same strict matching call and the same slot
lists (exposed names, areas, floors, satellite area as context) that Home
Assistant's default agent uses (components/conversation/default_agent.py,
2026.10.4). It does not talk to Home Assistant.

prefer_local_intents semantics read from the 2026.10.4 source: when the pipeline's
agent is an LLM with control, matches of HassGetState and HassMediaSearchAndPlay
are ignored (they interfere with the LLM); a handler error of a "no match" kind
(unknown name or area, no entity in the required state) is ignored too and the
text goes to the agent; only successes and FAILED_TO_HANDLE/UNKNOWN errors answer.

Needs (pinned to what HA 2026.10.4 declares): hassil==3.12.1 and
home-assistant-intents==2026.10.6, e.g. in a throw-away venv:
  python3 -m venv /tmp/v && /tmp/v/bin/pip install hassil==3.12.1 home-assistant-intents==2026.10.6 pyyaml
  /tmp/v/bin/python scripts/voice-assistant/agent_eval/builtin_intents.py

The result is an inference: hassil recognition plus this repository's fake
target resolver, not a run of the real intent handlers. Label as inferred.
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import grade  # noqa: E402
import ha_fake  # noqa: E402
import run as ae  # noqa: E402


def remove_punctuation(text):
    return re.sub(r"[¿?¡!.,;:]", " ", text).strip()


def build_matcher(home):
    from hassil import Intents, recognize_best
    from hassil.intents import TextSlotList
    import home_assistant_intents as hai

    data = hai.get_intents("es")
    intents = Intents.from_dict(data)
    names = []
    for eid, e in home.entities.items():
        ctx = {"domain": eid.split(".")[0]}
        dc = e.get("device_class") or e.get("attributes", {}).get("device_class")
        if dc:
            ctx["device_class"] = dc
        for n in [e["name"]] + e.get("aliases", []):
            names.append((remove_punctuation(n), n, ctx))
    areas, floors = [], []
    for a in home.areas.values():
        for n in [a["name"]] + a.get("aliases", []):
            areas.append((remove_punctuation(n), n))
    for f in home.floors.values():
        floors.append((remove_punctuation(f["name"]), f["name"]))
    slot_lists = {"name": TextSlotList.from_tuples(names, allow_template=False),
                  "area": TextSlotList.from_tuples(areas, allow_template=False),
                  "floor": TextSlotList.from_tuples(floors, allow_template=False)}
    sat = home.areas[home.satellite_area]["name"]
    ctx = {"area": {"value": sat, "text": sat}}

    def match(text):
        return recognize_best(text, intents, slot_lists=slot_lists, intent_context=ctx, language="es",
                              best_slot_name="name")
    return match


# Intents Home Assistant ignores under prefer_local_intents when the agent is an LLM with control
# (_async_local_fallback_intent_filter in assist_pipeline, 2026.10.4).
LLM_IGNORED = {"HassGetState", "HassMediaSearchAndPlay"}
# Intents with a spoken local answer but no LLM tool twin (answer taken from entity state).
LOCAL_ONLY = {"HassClimateGetTemperature", "HassGetCurrentTime", "HassGetCurrentDate", "HassGetWeather"}


def to_call(result, home, tool_names):
    """Map a recognition result to a fake tool call (name, args, meta)."""
    intent = result.intent.name
    tool = next((t for t in tool_names if t.endswith("__" + intent)), None)
    args = {}
    for k, ent in result.entities.items():
        v = ent.value
        if k in ("domain", "device_class"):
            v = [v]
        args[k] = v
    if "volume_level" in args or "volume" in args:
        args["volume_level"] = args.pop("volume_level", args.pop("volume", None))
    return intent, tool, args


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phrases", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    home = ha_fake.FakeHome()
    match = build_matcher(home)
    tool_names = [t["function"]["name"] for t in ha_fake.build_tools()]
    rows, stats = [], {}
    for item in ae.load_phrases(args.phrases):
        if item["category"] not in ("control", "live"):
            continue
        spec = item["turns"][0]
        res = match(remove_punctuation(spec["text"]))
        if res is None:
            outcome, detail = "no match", ""
        else:
            intent, tool, slots = to_call(res, home, tool_names)
            detail = f"{intent} {{{', '.join(f'{k}={v}' for k, v in slots.items())}}}"
            if intent in LLM_IGNORED:
                outcome = "ignored by HA (goes to the LLM agent)"
            elif tool is None and intent in LOCAL_ONLY:
                outcome = "handled locally (no LLM-tool twin)"
            elif tool is None:
                outcome = "matched, no LLM-tool twin"
            else:
                result, meta = home.execute(tool, slots)
                call = {"name": tool, "args": slots, "ok": meta["ok"], "targets": meta["targets"], "meta_args": meta["args"]}
                exp = {k: v for k, v in (spec["expect"].get("any_of") and {"any_of": spec["expect"]["any_of"]} or spec["expect"]).items()}
                g = grade.grade_turn(_strip_answer(exp), {"rounds": [{"tool_calls": [call]}], "final_text": "?"},
                                     ha_fake.READ_ONLY_TOOLS)
                if not meta["ok"]:
                    outcome = "matched, target not found: HA falls back to the LLM agent"
                else:
                    outcome = {"pass": "handled, correct target", "fail": "handled, WRONG target",
                               "review": "handled (not graded)"}.get(g["status"], g["status"])
        stats[(item["category"], outcome)] = stats.get((item["category"], outcome), 0) + 1
        rows.append((item["id"], spec["text"], outcome, detail))
    for r in rows:
        if args.verbose or True:
            print(f"{r[0]:5} {r[2]:60} {r[1]}  -> {r[3]}")
    print()
    for k in sorted(stats):
        print(k, stats[k])


def _strip_answer(exp):
    def strip(alt):
        return {k: v for k, v in alt.items() if not k.startswith(("answer", "must_not", "limitation", "no_success", "tools_any"))}
    if "any_of" in exp:
        return {"any_of": [strip(a) for a in exp["any_of"]]}
    return strip(exp)


if __name__ == "__main__":
    main()
