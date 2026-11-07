#!/usr/bin/env python3
"""Built-in intent coverage and miss categories on kivi-fake-v1 (text and audio subset).

For every item, Home Assistant's built-in Spanish sentences (hassil 3.12.1 + home-assistant-intents
2026.10.6, slot lists of the item's fake home, satellite area as context, prefer_local_intents
semantics as in agent_eval/builtin_intents.py) either act or fall through to the agent. The action
is compared with the gold action (schema.canonical: intent, resolved entity set, value).

  correct   gold route execute and the built-in action equals the gold action
  wrong     the built-in intents act and the action differs (or the gold route is kiais)
  local     answered locally without a device action (time, date, timer status)
  miss      falls through to the conversation agent (the decider's input)

Audio misses of execute items get the categories of headroom.py (a, a-verb, b1, b2, c), with the
item text as reference. --custom adds a custom sentences file (Home Assistant's
custom_sentences/es format) on top of the built-in sentences, to measure what a cheap fix covers.

  python headroom_v1.py --data $POC/data/kivi-fake-v1 [--custom custom_sentences_es.yaml] [--audio]
"""
import argparse
import collections
import csv
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "agent_eval"))
import headroom as hr  # noqa: E402
import schema as sc  # noqa: E402

HA_TO_LABEL = {"HassTurnOn": "turn_on", "HassTurnOff": "turn_off", "HassSetPosition": "cover_position",
               "HassStopMoving": "stop_cover", "HassLightSet": "light_set", "HassSetVolume": "set_volume", "HassSetVolumeRelative": "volume_up",
               "HassMediaPlayerMute": "mute", "HassMediaPlayerUnmute": "unmute", "HassMediaPause": "pause",
               "HassMediaUnpause": "resume", "HassMediaNext": "next", "HassMediaPrevious": "previous",
               "HassClimateSetTemperature": "climate_set", "HassFanSetSpeed": "fan_speed",
               "HassStartTimer": "start_timer", "HassCancelTimer": "cancel_timer", "HassCancelAllTimers": "cancel_timer"}
VALUE_KEY = {"cover_position": "position", "light_set": "brightness", "set_volume": "volume_level",
             "climate_set": "temperature", "fan_speed": "percentage", "start_timer": "total_seconds"}
_INTENTS_CACHE = {}


def load_intents(custom=None):
    """Built-in Spanish sentences, or (custom given) ONLY the custom sentences with the built-in
    lists and expansion rules available to them."""
    import home_assistant_intents as hai
    from hassil import Intents
    key = str(custom)
    if key not in _INTENTS_CACHE:
        data = hai.get_intents("es")
        if custom:
            extra = yaml.safe_load(open(custom, encoding="utf-8"))
            data = {"language": "es", "intents": extra["intents"], "lists": {**data["lists"], **extra.get("lists", {})},
                    "expansion_rules": {**data["expansion_rules"], **extra.get("expansion_rules", {})}}
        _INTENTS_CACHE[key] = Intents.from_dict(data)
    return _INTENTS_CACHE[key]


def build_matcher(home, custom=None):
    """builtin_intents.build_matcher with cached sentence data and optional custom sentences."""
    import builtin_intents as bi
    from hassil import recognize_best
    from hassil.intents import TextSlotList
    intents = load_intents()
    custom_intents = load_intents(custom) if custom else None
    names, areas, floors = [], [], []
    for eid, e in home.entities.items():
        ctx = {"domain": eid.split(".")[0]}
        dc = e.get("device_class") or e.get("attributes", {}).get("device_class")
        if dc:
            ctx["device_class"] = dc
        for n in [e["name"]] + e.get("aliases", []):
            names.append((bi.remove_punctuation(n), n, ctx))
    for a in home.areas.values():
        for n in [a["name"]] + a.get("aliases", []):
            areas.append((bi.remove_punctuation(n), n))
    for f in home.floors.values():
        floors.append((bi.remove_punctuation(f["name"]), f["name"]))
    slot_lists = {"name": TextSlotList.from_tuples(names, allow_template=False),
                  "area": TextSlotList.from_tuples(areas, allow_template=False),
                  "floor": TextSlotList.from_tuples(floors, allow_template=False)}
    sat = home.areas[home.satellite_area]["name"]
    ctx = {"area": {"value": sat, "text": sat}}
    def match(text):
        # Home Assistant's default agent tries the user's custom sentences before the built-in ones.
        if custom_intents is not None:
            r = recognize_best(text, custom_intents, slot_lists=slot_lists, intent_context=ctx, language="es",
                               best_slot_name="name")
            if r is not None:
                return r
        return recognize_best(text, intents, slot_lists=slot_lists, intent_context=ctx, language="es",
                              best_slot_name="name")
    return match


class Builtin:
    def __init__(self, data, custom=None):
        import ha_fake
        self.homes, self.match = {}, {}
        self.tools = [t["function"]["name"] for t in ha_fake.build_tools()]
        self.read_only = ha_fake.READ_ONLY_TOOLS
        for p in sorted((Path(data) / "homes").glob("*.yaml")):
            h = ha_fake.FakeHome(str(p))
            self.homes[p.stem] = h
            self.match[p.stem] = build_matcher(h, custom)
        self.cache = {}

    def run(self, home_id, text):
        """('miss' | 'local' | 'act', Action or None, detail)"""
        key = (home_id, text)
        if key in self.cache:
            return self.cache[key]
        import builtin_intents as bi
        home = self.homes[home_id]
        res = self.match[home_id](bi.remove_punctuation(text))
        if res is None:
            out = ("miss", None, "no match")
        else:
            intent, tool, slots = bi.to_call(res, home, self.tools)
            if intent in bi.LLM_IGNORED:
                out = ("miss", None, f"{intent} ignored")
            elif tool is None or tool in self.read_only:
                out = ("local", None, intent)
            else:
                _r, meta = home.execute(tool, slots)
                if not meta["ok"]:
                    out = ("miss", None, f"{intent} target not found")
                else:
                    out = ("act", to_action(intent, meta), f"{intent} {slots}")
        self.cache[key] = out
        return out


def to_action(ha_intent, meta):
    label = HA_TO_LABEL.get(ha_intent)
    if label is None:
        return sc.Action(ha_intent, frozenset(meta["targets"]), None)
    args = meta.get("args") or {}
    if ha_intent == "HassSetVolumeRelative":
        label = "volume_up" if args.get("volume_dir") == "up" else "volume_down"
    value = args.get(VALUE_KEY.get(label, ""), None)
    if label == "climate_set" and value is not None:
        value = float(value)
    if label == "start_timer":
        return sc.canonical(label, (), value)
    return sc.canonical(label, meta["targets"], value)


def gold_action(item):
    a, ents = item["actions"][0], item["entities"][0]
    v = a["value"]
    if a["intent"] == "climate_set" and v is not None:
        v = float(v)
    return sc.canonical(a["intent"], ents, v)


def outcome(bt, item, text):
    kind, act, detail = bt.run(item["home"], text)
    if kind != "act":
        return kind, detail
    if item["route"] == "execute" and act == gold_action(item):
        return "correct", detail
    return "wrong", detail


def table(rows, key, title):
    print(f"\n{title}\n")
    print("| group | n | built-in correct | built-in wrong | local | miss |")
    print("| --- | ---: | ---: | ---: | ---: | ---: |")
    groups = collections.defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    for g in sorted(groups, key=str) + ["all"]:
        rs = rows if g == "all" else groups[g]
        n = len(rs)
        c = collections.Counter(r["outcome"] for r in rs)
        pc = lambda v: f"{v} ({v / n:.0%})"  # noqa: E731
        print(f"| {g} | {n} | {pc(c['correct'])} | {pc(c['wrong'])} | {pc(c['local'])} | {pc(c['miss'])} |")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--items", default="items.jsonl")
    ap.add_argument("--custom", default=None)
    ap.add_argument("--audio", action="store_true", help="also score stt_fake.csv transcripts")
    ap.add_argument("--dump", default=None)
    ap.add_argument("--samples", type=int, default=0)
    a = ap.parse_args()
    items = [json.loads(x) for x in open(a.data / a.items, encoding="utf-8")]
    bt = Builtin(a.data, a.custom)
    rows = []
    for it in items:
        o, d = outcome(bt, it, it["text"])
        rows.append(dict(id=it["id"], home=it["home"], style=it["style"], kind=it["kind"], route=it["route"],
                         source=it["source"], text=it["text"], outcome=o, detail=d))
    ex = [r for r in rows if r["route"] == "execute"]
    ki = [r for r in rows if r["route"] == "kiais"]
    print(f"kivi-fake-v1 text: {len(rows)} items ({len(ex)} execute, {len(ki)} kiais); custom sentences: {a.custom}")
    table(ex, lambda r: f"{r['style']} ({r['source']})" if r["source"] == "llm" else r["style"],
          "Execute items (commands) by style, text:")
    table(ex, lambda r: r["home"], "Execute items by home, text:")
    table(ki, lambda r: f"{r['kind']}/{r['style']}", "Route-kiais items (must not act), text:")
    if a.samples:
        import random
        rng = random.Random(1)
        for o in ("miss", "wrong"):
            xs = [r for r in rows if r["outcome"] == o]
            print(f"\nSample {o} ({len(xs)}):")
            for r in rng.sample(xs, min(a.samples, len(xs))):
                print(f"  {r['id']:18} {r['kind']:8} {r['style']:16} {r['text']!r}  [{r['detail']}]")
    if a.audio:
        stt = list(csv.DictReader(open(a.data / "stt_fake.csv")))
        by_id = {it["id"]: it for it in items}
        text_ok = {r["id"]: r["outcome"] == "correct" for r in rows}
        arows = []
        stt = [s for s in stt if s["id"] in by_id]  # items dropped by a later re-validation
        for s in stt:
            it = by_id[s["id"]]
            o, d = outcome(bt, it, s["hyp"])
            cat = ""
            if it["route"] == "execute" and o == "miss":
                kw, _det = hr.keyword_status(it["text"], s["hyp"])
                cat = hr.category(text_ok[it["id"]], kw)
            arows.append(dict(id=s["id"], cond=s["cond"], route=it["route"], style=it["style"], outcome=o, cat=cat,
                              text=it["text"], hyp=s["hyp"]))
        print("\nAudio subset (synthetic Piper voices, TV content), execute items:\n")
        print("| cond | n | built-in correct | wrong | miss | a misheard | a-verb | b1 phrasing | b2 near-miss | c other |"
              " b1+b2 x 3 s per command |")
        print("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for cond in ["clean", "snr+5", "snr+0", "all"]:
            rs = [r for r in arows if r["route"] == "execute" and (cond == "all" or r["cond"] == cond)]
            n = len(rs)
            c = collections.Counter(r["outcome"] for r in rs)
            k = collections.Counter(r["cat"] for r in rs)
            pc = lambda v: f"{v} ({v / n:.0%})"  # noqa: E731
            print(f"| {cond} | {n} | {pc(c['correct'])} | {pc(c['wrong'])} | {pc(c['miss'])} | {pc(k['a'])} | "
                  f"{pc(k['a-verb'])} | {pc(k['b1'])} | {pc(k['b2'])} | {pc(k['c'])} | "
                  f"{(k['b1'] + k['b2']) * 3 / n:.2f} s |")
        print("\nAudio subset, route-kiais items: built-in acted (wrong) per condition:",
              {cond: sum(1 for r in arows if r["route"] == "kiais" and r["cond"] == cond and r["outcome"] == "wrong")
               for cond in ["clean", "snr+5", "snr+0"]},
              "of", sum(1 for r in arows if r["route"] == "kiais" and r["cond"] == "clean"), "each")
        if a.samples:
            import random
            rng = random.Random(2)
            for cat in hr.CATS:
                xs = [r for r in arows if r["cat"] == cat]
                print(f"\nSample audio {cat} ({len(xs)}):")
                for r in rng.sample(xs, min(a.samples, len(xs))):
                    print(f"  {r['id']:18} {r['cond']:6} {r['text']!r} -> {r['hyp']!r}")
        if a.dump:
            with open(Path(a.dump).with_suffix(".audio.csv"), "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(arows[0]))
                w.writeheader()
                w.writerows(arows)
    if a.dump:
        with open(a.dump, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)


if __name__ == "__main__":
    main()
