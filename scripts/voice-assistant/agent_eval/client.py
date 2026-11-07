"""Minimal streaming client for OpenAI-compatible /chat/completions endpoints
(Kiais, Ollama's /v1, and any other). Standard library only.

It measures, per request, the time to the first content or tool-call token
(TTFT), the time to the first token of any kind (including reasoning), and the
total time. The API key is read by the caller and is never written to results.
"""
import json
import re
import time
import urllib.error
import urllib.request

# Response headers worth keeping to learn which backend served a request.
HEADER_KEEP = re.compile(r"(backend|upstream|provider|served|route|model|x-request-id|x-kiais|x-isidoro|x-ratelimit)", re.I)


class BudgetExceeded(RuntimeError):
    pass


class RequestBudget:
    """Counts requests and refuses to exceed a cap (per run and across runs)."""

    def __init__(self, run_cap, ledger_total=0, total_cap=None):
        self.run_cap = run_cap
        self.count = 0
        self.ledger_total = ledger_total
        self.total_cap = total_cap

    def take(self):
        if self.run_cap is not None and self.count >= self.run_cap:
            raise BudgetExceeded(f"run cap of {self.run_cap} requests reached")
        if self.total_cap is not None and self.ledger_total + self.count >= self.total_cap:
            raise BudgetExceeded(f"total budget of {self.total_cap} requests reached (ledger {self.ledger_total})")
        self.count += 1


def parse_sse_lines(lines):
    """Yield decoded JSON objects from an iterable of SSE byte lines."""
    for raw in lines:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


def chat_stream(base_url, model, messages, tools=None, api_key=None, timeout=90.0,
                temperature=0.2, max_tokens=400, extra_body=None, include_usage=True):
    """One streaming chat completion. Never raises on HTTP/network errors; the
    outcome is in the returned dict (``error`` is None on success)."""
    body = {"model": model, "messages": messages, "stream": True, "temperature": temperature,
            "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
    if include_usage:
        body["stream_options"] = {"include_usage": True}
    if extra_body:
        body.update(extra_body)
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    out = {"content": "", "tool_calls": [], "ttft_s": None, "ttft_any_s": None, "ttfb_s": None, "total_s": None,
           "finish_reason": None, "model_field": None, "usage": None, "headers": {}, "http_status": None,
           "error": None}
    t0 = time.monotonic()
    calls = {}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out["http_status"] = resp.status
            out["ttfb_s"] = time.monotonic() - t0
            out["headers"] = {k.lower(): v for k, v in resp.headers.items() if HEADER_KEEP.search(k)}
            for chunk in parse_sse_lines(resp):
                now = time.monotonic() - t0
                if chunk.get("model"):
                    out["model_field"] = chunk["model"]
                if chunk.get("usage"):
                    out["usage"] = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if choice.get("finish_reason"):
                        out["finish_reason"] = choice["finish_reason"]
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning and out["ttft_any_s"] is None:
                        out["ttft_any_s"] = now
                    text = delta.get("content")
                    if text:
                        out["content"] += text
                        if out["ttft_s"] is None:
                            out["ttft_s"] = now
                        if out["ttft_any_s"] is None:
                            out["ttft_any_s"] = now
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        # OpenAI sends the name once; some backends behind the gateway repeat the
                        # whole name in every chunk (seen on Kiais), so only append a different piece.
                        if fn.get("name") and fn["name"] != slot["name"]:
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
                        if out["ttft_s"] is None:
                            out["ttft_s"] = now
                        if out["ttft_any_s"] is None:
                            out["ttft_any_s"] = now
    except urllib.error.HTTPError as exc:
        out["http_status"] = exc.code
        out["error"] = f"http {exc.code}"
        try:
            out["error_body"] = exc.read(500).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        finally:
            exc.close()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:200]
    out["total_s"] = time.monotonic() - t0
    for idx in sorted(calls):
        c = calls[idx]
        try:
            args = json.loads(c["arguments"]) if c["arguments"].strip() else {}
        except json.JSONDecodeError:
            args = None
        out["tool_calls"].append({"id": c["id"] or f"call_{idx}", "name": c["name"], "arguments": c["arguments"],
                                  "args": args})
    return out
