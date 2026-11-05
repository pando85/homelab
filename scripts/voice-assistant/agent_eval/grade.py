"""Automatic grading of one conversation turn against an expectation from
phrases_es.yaml. Pure functions; no network.

A turn record looks like:
  {"rounds": [{"tool_calls": [{"name", "args", "ok", "targets", "meta_args"}], ...}, ...],
   "final_text": "...", "error": None}
"""
import re

LIMITATION_RE = re.compile(
    r"(?i)(no (puedo|podría|tengo|dispongo|sé|cuento|me es posible|soy capaz|estoy conectad)"
    r"|no (hay|dispongo de|tengo) (información|datos|acceso|forma|manera)"
    r"|no (es )?posible|desconozco|sin acceso)")
CLAIM_RE = re.compile(r"(?i)(he (encendido|apagado|puesto|subido|bajado|abierto|cerrado|activado)|ya está|hecho\b|listo\b"
                      r"|encendid[ao]|apagad[ao])")
MARKDOWN_RE = re.compile(r"(\*\*|__|`|^\s*#{1,6}\s|^\s*[-*•]\s+\S|^\s*\d+[.)]\s+\S|\[[^\]]+\]\([^)]+\))", re.M)
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐️]")
ES_WORDS = {"el", "la", "los", "las", "de", "que", "en", "y", "es", "un", "una", "por", "con", "no", "para", "se", "su",
            "al", "lo", "como", "más", "hay", "está", "son", "del", "he", "te", "me", "puedo", "tengo", "ahora"}
EN_WORDS = {"the", "is", "are", "of", "and", "to", "in", "that", "it", "for", "you", "with", "this", "can", "have",
            "i", "was", "at", "on", "not", "your", "will"}


def format_check(text):
    """Spoken-answer hygiene: plain Spanish text, no markdown, no emoji, correct name."""
    text = text or ""
    words = re.findall(r"[a-záéíóúñü]+", text.lower())
    es = sum(w in ES_WORDS for w in words)
    en = sum(w in EN_WORDS for w in words)
    issues = []
    if MARKDOWN_RE.search(text):
        issues.append("markdown")
    if EMOJI_RE.search(text):
        issues.append("emoji")
    if len(words) >= 4 and en > es:
        issues.append("not_spanish")
    if re.search(r"(?i)\bkiwi\b", text):
        issues.append("name_kiwi")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    return {"issues": issues, "ok": not issues, "chars": len(text), "sentences": len(sentences),
            "long": len(sentences) > 3 or len(text) > 300}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def match_arg(expected, actual):
    """expected: number, '>=N', '<=N', string, or a list of alternatives."""
    if isinstance(expected, list):
        return any(match_arg(e, actual) for e in expected)
    if isinstance(expected, str):
        m = re.fullmatch(r"(>=|<=)\s*(-?\d+(?:\.\d+)?)", expected.strip())
        if m:
            a = _num(actual)
            return a is not None and (a >= float(m.group(2)) if m.group(1) == ">=" else a <= float(m.group(2)))
        return str(actual).strip().casefold() == expected.strip().casefold()
    n, a = _num(expected), _num(actual)
    if n is not None and a is not None:
        return abs(n - a) < 1e-6
    return expected == actual


def _call_matches(req, call):
    if not call.get("ok"):
        return False
    tools = req.get("tool")
    tools = [tools] if isinstance(tools, str) else (tools or [])
    if tools and call["name"] not in tools:
        return False
    tg = set(call.get("targets") or [])
    if "targets" in req and tg != set(req["targets"]):
        return False
    if "targets_include" in req and not set(req["targets_include"]) <= tg:
        return False
    if "targets_within" in req and not (tg and tg <= set(req["targets_within"])):
        return False
    for key, exp in (req.get("args") or {}).items():
        merged = {**(call.get("args") or {}), **(call.get("meta_args") or {})}
        if key not in merged or not match_arg(exp, merged[key]):
            return False
    return True


def _is_state_changing(call, read_only):
    return call["name"] not in read_only


def eval_alt(alt, calls, text, read_only):
    """Return (ok, reason) for one alternative."""
    text = text or ""
    if alt.get("clarify"):
        if any(c.get("ok") and _is_state_changing(c, read_only) for c in calls):
            return False, "acted instead of asking"
        return ("?" in text), ("" if "?" in text else "no clarifying question")
    if alt.get("no_tool") and calls:
        return False, "called a tool"
    reqs = alt.get("calls") or ([alt] if alt.get("tool") else [])
    used = set()
    for req in reqs:
        idx = next((i for i, c in enumerate(calls) if i not in used and _call_matches(req, c)), None)
        if idx is None:
            return False, "expected call not found (tool/targets/args)"
        used.add(idx)
    if reqs:
        extras = [c for i, c in enumerate(calls) if i not in used and c.get("ok") and _is_state_changing(c, read_only)]
        if extras:
            return False, "extra state-changing call: " + extras[0]["name"]
    if alt.get("tools_any") and not any(c["name"] in alt["tools_any"] for c in calls):
        return False, "expected tool not called"
    if alt.get("no_success_claim"):
        if any(c.get("ok") and _is_state_changing(c, read_only) for c in calls):
            return False, "state-changing call succeeded on a missing target"
        if CLAIM_RE.search(text):
            return False, "claims success"
    if alt.get("answer_regex") and not re.search(alt["answer_regex"], text):
        return False, "answer_regex not found"
    for rx in alt.get("answer_regex_all") or []:
        if not re.search(rx, text):
            return False, "answer_regex_all missing: " + rx
    if alt.get("must_not_regex") and re.search(alt["must_not_regex"], text):
        return False, "forbidden pattern present (invented data)"
    if alt.get("limitation") and not LIMITATION_RE.search(text):
        return False, "does not state it cannot know/consult this"
    return True, ""


def grade_turn(expect, turn, read_only):
    """Return {"status": pass|fail|review|error, "reason", "first_round_ok"}."""
    if turn.get("error"):
        return {"status": "error", "reason": turn["error"], "first_round_ok": False}
    alts = expect["any_of"] if "any_of" in expect else [expect]
    if all(a.get("review") for a in alts):
        return {"status": "review", "reason": "manual", "first_round_ok": None}
    rounds = turn.get("rounds") or []
    all_calls = [c for r in rounds for c in r.get("tool_calls", [])]
    first_calls = list(rounds[0].get("tool_calls", [])) if rounds else []
    text = turn.get("final_text") or ""
    reasons = []
    ok_all = False
    for alt in alts:
        ok, why = eval_alt(alt, all_calls, text, read_only)
        reasons.append(why)
        ok_all = ok_all or ok
    # first-round correctness: calls of the first round only, answer checks skipped
    # (the answer is produced after the tool result), meaningful for action expectations.
    first_ok = None
    if any(a.get("tool") or a.get("calls") for a in alts):
        first_ok = any(eval_alt({k: v for k, v in a.items() if not k.startswith(("answer", "must_not", "limitation"))},
                                first_calls, "", read_only)[0]
                       for a in alts if a.get("tool") or a.get("calls"))
    return {"status": "pass" if ok_all else "fail", "reason": "" if ok_all else "; ".join(r for r in reasons if r),
            "first_round_ok": first_ok}
