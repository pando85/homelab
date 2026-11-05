"""Self-tests of scripts/voice-assistant/agent_eval (no real endpoint: a local scripted server).

Run: python3 -m unittest discover -s scripts/voice-assistant/agent_eval -p 'test_agent_eval.py'
"""
import http.server
import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client  # noqa: E402
import grade  # noqa: E402
import ha_fake  # noqa: E402
import run as ae  # noqa: E402

RO = ha_fake.READ_ONLY_TOOLS


def sse(*chunks):
    body = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


def text_chunks(text, model="fake-model"):
    return [{"model": model, "choices": [{"delta": {"content": text[:5]}}]},
            {"model": model, "choices": [{"delta": {"content": text[5:]}, "finish_reason": "stop"}]},
            {"model": model, "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4}}]


def tool_chunks(name, args, call_id="call_1"):
    a = json.dumps(args)
    return [{"model": "fake-model", "choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": call_id, "function": {"name": name, "arguments": a[:6]}}]}}]},
            {"model": "fake-model", "choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": a[6:]}}]}, "finish_reason": "tool_calls"}]}]


class FakeServer:
    """Serves queued SSE bodies; records the request bodies it received."""

    def __init__(self, responses, delay=0.0, status=200):
        self.responses, self.delay, self.status, self.requests = list(responses), delay, status, []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                       "body": json.loads(self.rfile.read(n))})
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"nope"}}')
                    return
                body = outer.responses.pop(0)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("x-served-by", "fake-backend")
                self.end_headers()
                time.sleep(outer.delay)
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class DataFiles(unittest.TestCase):
    def test_phrases_reference_real_tools_and_entities(self):
        home = ha_fake.FakeHome()
        names = {t["function"]["name"] for t in ha_fake.build_tools()}
        self.assertEqual(len(names), len(ha_fake.build_tools()))
        items = ae.load_phrases()
        ids = [i["id"] for i in items]
        self.assertEqual(len(ids), len(set(ids)))

        def walk(exp):
            for alt in exp.get("any_of", [exp]):
                for req in alt.get("calls", [alt]):
                    tools = req.get("tool", [])
                    for t in ([tools] if isinstance(tools, str) else tools):
                        self.assertIn(t, names, t)
                    for key in ("targets", "targets_include", "targets_within"):
                        for e in req.get(key, []):
                            self.assertIn(e, home.entities, e)
                for t in alt.get("tools_any", []):
                    self.assertIn(t, names, t)

        for it in items:
            for turn in it["turns"]:
                walk(turn["expect"])

    def test_counts(self):
        cats = {}
        for it in ae.load_phrases():
            cats[it["category"]] = cats.get(it["category"], 0) + 1
        self.assertGreaterEqual(cats["control"], 40)
        self.assertGreaterEqual(cats["general"], 15)
        self.assertGreaterEqual(cats["live"], 10)
        self.assertGreaterEqual(cats["followup"], 6)

    def test_tool_schemas_are_openai_shaped(self):
        for t in ha_fake.build_tools():
            self.assertEqual(t["type"], "function")
            self.assertEqual(t["function"]["parameters"]["type"], "object")
            json.dumps(t)

    def test_prompt_names_kivi(self):
        p = ha_fake.FakeHome().system_prompt()
        self.assertIn("Kivi", p)
        self.assertNotIn("Kiwi", p)
        self.assertIn("You are in area Salón", p)


class FakeHomeTests(unittest.TestCase):
    def setUp(self):
        self.h = ha_fake.FakeHome()

    def test_area_and_domain(self):
        res, meta = self.h.execute("intent__HassTurnOn", {"area": "Salón", "domain": ["light"]})
        self.assertTrue(meta["ok"])
        self.assertEqual(set(meta["targets"]), {"light.salon_techo", "light.salon_lampara"})
        self.assertEqual(res["response_type"], "action_done")

    def test_duplicate_name_prefers_satellite_area(self):
        _, meta = self.h.execute("intent__HassTurnOn", {"name": "Luz del techo"})
        self.assertEqual(meta["targets"], ["light.salon_techo"])

    def test_unknown_area_errors_like_ha(self):
        res, meta = self.h.execute("intent__HassTurnOn", {"area": "Garaje", "domain": ["light"]})
        self.assertFalse(meta["ok"])
        self.assertEqual(res["error"], "MatchFailedError")

    def test_no_slots_cannot_target_all(self):
        res, meta = self.h.execute("intent__HassTurnOff", {})
        self.assertFalse(meta["ok"])

    def test_timer_total_seconds_and_volume_direction(self):
        _, m = self.h.execute("intent__HassStartTimer", {"hours": 1, "minutes": 30})
        self.assertEqual(m["args"]["total_seconds"], 5400)
        _, m = self.h.execute("media_player__HassSetVolumeRelative", {"volume_step": "down"})
        self.assertEqual((m["args"]["volume_dir"], m["targets"]), ("down", ["media_player.tele_salon"]))

    def test_pause_requires_playing(self):
        _, m = self.h.execute("media_player__HassMediaPause", {"name": "Tele"})
        self.assertTrue(m["ok"])

    def test_live_context_filters(self):
        res, m = self.h.execute("homeassistant__GetLiveContext", {"area": "Salón", "domain": "sensor"})
        self.assertIn("22.4", res["result"])
        self.assertNotIn("Cocina", res["result"])

    def test_area_wide_off_skips_sensors(self):
        _, m = self.h.execute("intent__HassTurnOff", {"area": "Cocina"})
        self.assertEqual(set(m["targets"]), {"light.cocina_techo", "light.cocina_encimera",
                                              "media_player.altavoz_cocina", "switch.cafetera"})

    def test_unknown_tool(self):
        res, m = self.h.execute("nope__Tool", {})
        self.assertFalse(m["ok"])


def call(name, ok=True, targets=(), meta_args=None, args=None):
    return {"name": name, "ok": ok, "targets": list(targets), "meta_args": meta_args or {}, "args": args or {}}


def turn(calls, text="", error=None):
    return {"rounds": [{"tool_calls": calls}], "final_text": text, "error": error}


class Grading(unittest.TestCase):
    def test_exact_targets_and_args(self):
        exp = {"tool": "light__HassLightSet", "targets": ["a", "b"], "args": {"brightness": 40}}
        good = turn([call("light__HassLightSet", targets=["b", "a"], meta_args={"brightness": 40})], "ok")
        self.assertEqual(grade.grade_turn(exp, good, RO)["status"], "pass")
        bad = turn([call("light__HassLightSet", targets=["a"], meta_args={"brightness": 40})])
        self.assertEqual(grade.grade_turn(exp, bad, RO)["status"], "fail")
        bad = turn([call("light__HassLightSet", targets=["a", "b"], meta_args={"brightness": 50})])
        self.assertEqual(grade.grade_turn(exp, bad, RO)["status"], "fail")

    def test_failed_call_does_not_count_but_retry_does(self):
        exp = {"tool": "intent__HassTurnOn", "targets": ["x"]}
        t = {"rounds": [{"tool_calls": [call("intent__HassTurnOn", ok=False)]},
                        {"tool_calls": [call("intent__HassTurnOn", targets=["x"])]}], "final_text": "ok", "error": None}
        g = grade.grade_turn(exp, t, RO)
        self.assertEqual(g["status"], "pass")
        self.assertFalse(g["first_round_ok"])

    def test_extra_action_fails(self):
        exp = {"tool": "intent__HassTurnOn", "targets": ["x"]}
        t = turn([call("intent__HassTurnOn", targets=["x"]), call("intent__HassTurnOff", targets=["y"])])
        self.assertEqual(grade.grade_turn(exp, t, RO)["status"], "fail")

    def test_any_of_and_clarify(self):
        exp = {"any_of": [{"tool": "intent__HassTurnOn", "targets": ["x"]}, {"clarify": True}]}
        self.assertEqual(grade.grade_turn(exp, turn([], "¿En qué habitación?"), RO)["status"], "pass")
        self.assertEqual(grade.grade_turn(exp, turn([], "Hecho."), RO)["status"], "fail")
        self.assertEqual(grade.grade_turn(exp, turn([call("intent__HassTurnOn", targets=["x"])], "ok"), RO)["status"], "pass")

    def test_answers_are_case_insensitive_and_device_by_device_is_tolerated(self):
        self.assertEqual(grade.grade_turn({"no_tool": True, "answer_regex": "29|veintinueve"},
                                          turn([], "Veintinueve días."), RO)["status"], "pass")
        exp = {"tool": "intent__HassTurnOff", "targets_include": ["a"], "targets_within": ["a", "b"]}
        t = turn([call("intent__HassTurnOff", targets=["a"]), call("intent__HassTurnOff", targets=["b"])])
        self.assertEqual(grade.grade_turn(exp, t, RO)["status"], "pass")
        t = turn([call("intent__HassTurnOff", targets=["a"]), call("intent__HassTurnOff", targets=["z"])])
        self.assertEqual(grade.grade_turn(exp, t, RO)["status"], "fail")

    def test_comparison_predicates(self):
        self.assertTrue(grade.match_arg(">=26", 30))
        self.assertFalse(grade.match_arg(">=26", 25))
        self.assertTrue(grade.match_arg("<=24", "20"))
        self.assertTrue(grade.match_arg([1, 2], 2.0))
        self.assertTrue(grade.match_arg("up", "UP"))

    def test_no_tool_and_regex(self):
        exp = {"no_tool": True, "answer_regex": "(?i)par[ií]s"}
        self.assertEqual(grade.grade_turn(exp, turn([], "París."), RO)["status"], "pass")
        self.assertEqual(grade.grade_turn(exp, turn([], "Lyon."), RO)["status"], "fail")
        self.assertEqual(grade.grade_turn(exp, turn([call("llm__GetDateTime")], "París."), RO)["status"], "fail")

    def test_limitation_and_invented_data(self):
        exp = {"limitation": True, "must_not_regex": r"\d[\d.,]*\s*(€|euros)"}
        self.assertEqual(grade.grade_turn(exp, turn([], "No puedo consultar la bolsa."), RO)["status"], "pass")
        self.assertEqual(grade.grade_turn(exp, turn([], "El bitcoin está a 60.000 euros."), RO)["status"], "fail")
        self.assertEqual(grade.grade_turn(exp, turn([], "Va a llover mañana."), RO)["status"], "fail")

    def test_review_and_error(self):
        self.assertEqual(grade.grade_turn({"review": True}, turn([], "x"), RO)["status"], "review")
        self.assertEqual(grade.grade_turn({"no_tool": True}, turn([], "", error="http 401"), RO)["status"], "error")

    def test_missing_target_claim(self):
        exp = {"no_success_claim": True, "answer_regex": "(?i)no (hay|existe)"}
        self.assertEqual(grade.grade_turn(exp, turn([call("intent__HassTurnOn", ok=False)], "No hay ninguna luz ahí."), RO)["status"], "pass")
        self.assertEqual(grade.grade_turn(exp, turn([call("intent__HassTurnOn", ok=False)], "Hecho, luz encendida. No hay problema"), RO)["status"], "fail")

    def test_format_check(self):
        self.assertTrue(grade.format_check("La capital de Francia es París.")["ok"])
        self.assertIn("markdown", grade.format_check("**París** es la capital")["issues"])
        self.assertIn("markdown", grade.format_check("- uno\n- dos")["issues"])
        self.assertIn("not_spanish", grade.format_check("The capital of France is Paris and it is nice")["issues"])
        self.assertIn("name_kiwi", grade.format_check("Soy Kiwi, tu asistente")["issues"])


class ClientAndRunner(unittest.TestCase):
    def test_streaming_text_ttft_and_headers(self):
        srv = FakeServer([sse(*text_chunks("Hola, soy Kivi."))], delay=0.2)
        try:
            r = client.chat_stream(srv.url, "fast", [{"role": "user", "content": "hola"}], api_key="sekret")
        finally:
            srv.close()
        self.assertIsNone(r["error"])
        self.assertEqual(r["content"], "Hola, soy Kivi.")
        self.assertGreaterEqual(r["ttft_s"], 0.19)
        self.assertGreaterEqual(r["total_s"], r["ttft_s"])
        self.assertEqual(r["headers"].get("x-served-by"), "fake-backend")
        self.assertEqual(r["model_field"], "fake-model")
        self.assertEqual(srv.requests[0]["auth"], "Bearer sekret")
        self.assertTrue(srv.requests[0]["body"]["stream"])

    def test_streaming_tool_call_assembled(self):
        srv = FakeServer([sse(*tool_chunks("intent__HassTurnOn", {"area": "Salón", "domain": ["light"]}))])
        try:
            r = client.chat_stream(srv.url, "fast", [{"role": "user", "content": "x"}])
        finally:
            srv.close()
        self.assertEqual(r["tool_calls"][0]["name"], "intent__HassTurnOn")
        self.assertEqual(r["tool_calls"][0]["args"], {"area": "Salón", "domain": ["light"]})
        self.assertIsNotNone(r["ttft_s"])

    def test_tool_name_repeated_in_every_chunk(self):
        chunks = [{"model": "m", "choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "c1", "function": {"name": "intent__HassTurnOn", "arguments": '{"area"'}}]}}]},
                  {"model": "m", "choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "c1", "function": {"name": "intent__HassTurnOn", "arguments": ': "Salón"}'}}]},
                    "finish_reason": "tool_calls"}]}]
        srv = FakeServer([sse(*chunks)])
        try:
            r = client.chat_stream(srv.url, "fast", [{"role": "user", "content": "x"}])
        finally:
            srv.close()
        self.assertEqual(r["tool_calls"][0]["name"], "intent__HassTurnOn")
        self.assertEqual(r["tool_calls"][0]["args"], {"area": "Salón"})

    def test_http_error_is_reported_not_raised(self):
        srv = FakeServer([], status=401)
        try:
            r = client.chat_stream(srv.url, "fast", [{"role": "user", "content": "x"}])
        finally:
            srv.close()
        self.assertEqual(r["error"], "http 401")
        self.assertEqual(r["http_status"], 401)

    def test_budget(self):
        b = client.RequestBudget(2)
        b.take()
        b.take()
        with self.assertRaises(client.BudgetExceeded):
            b.take()
        b = client.RequestBudget(None, ledger_total=599, total_cap=600)
        b.take()
        with self.assertRaises(client.BudgetExceeded):
            b.take()

    def run_item(self, item_id, responses):
        item = next(i for i in ae.load_phrases() if i["id"] == item_id)
        srv = FakeServer(responses)
        home, tools = ha_fake.FakeHome(), ha_fake.build_tools()
        try:
            send = lambda m: client.chat_stream(srv.url, "fast", m, tools)  # noqa: E731
            turns = ae.run_conversation(item, home, tools, send, client.RequestBudget(20))
        finally:
            srv.close()
        return turns, srv

    def test_control_flow_with_fake_tool_result(self):
        turns, srv = self.run_item("c01", [
            sse(*tool_chunks("intent__HassTurnOn", {"area": "Salón", "domain": ["light"]})),
            sse(*text_chunks("Listo, luces del salón encendidas."))])
        t = turns[0]
        self.assertEqual(t["grade"]["status"], "pass")
        self.assertTrue(t["grade"]["first_round_ok"])
        self.assertEqual(t["timing"]["rounds"], 2)
        self.assertGreaterEqual(t["timing"]["total_s"], t["timing"]["ttfs_s"])
        second = srv.requests[1]["body"]["messages"]
        self.assertEqual(second[-1]["role"], "tool")
        self.assertIn("action_done", second[-1]["content"])
        self.assertEqual(second[-2]["tool_calls"][0]["function"]["name"], "intent__HassTurnOn")

    def test_wrong_target_fails(self):
        turns, _ = self.run_item("c01", [
            sse(*tool_chunks("intent__HassTurnOn", {"name": "Luz del techo"})),
            sse(*text_chunks("Hecho."))])
        self.assertEqual(turns[0]["grade"]["status"], "fail")

    def test_followup_keeps_history(self):
        turns, srv = self.run_item("f05", [
            sse(*text_chunks("Gabriel García Márquez.")), sse(*text_chunks("En mil novecientos sesenta y siete."))])
        self.assertEqual([t["grade"]["status"] for t in turns], ["pass", "pass"])
        msgs = srv.requests[1]["body"]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "user"])

    def test_error_stops_conversation(self):
        item = next(i for i in ae.load_phrases() if i["id"] == "f05")
        srv = FakeServer([], status=500)
        try:
            turns = ae.run_conversation(item, ha_fake.FakeHome(), [], lambda m: client.chat_stream(srv.url, "x", m),
                                        client.RequestBudget(5))
        finally:
            srv.close()
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["grade"]["status"], "error")


class Stats(unittest.TestCase):
    def test_percentiles(self):
        self.assertEqual(ae.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50), 5)
        self.assertEqual(ae.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95), 10)
        self.assertIsNone(ae.pct([None], 50))

    def test_plan_repeats_only_control(self):
        items = ae.load_phrases()
        jobs = ae.plan_items(items, list(ae.CATEGORIES), 2, 0, 1)
        ctl = sum(i["category"] == "control" for i in items)
        self.assertEqual(len(jobs), len(items) + ctl)

    def test_summary_table(self):
        item = next(i for i in ae.load_phrases() if i["id"] == "g01")
        srv = FakeServer([sse(*text_chunks("París."))])
        try:
            turns = ae.run_conversation(item, ha_fake.FakeHome(), [], lambda m: client.chat_stream(srv.url, "x", m),
                                        client.RequestBudget(5))
        finally:
            srv.close()
        rec = {"label": "t", "category": "general", "kind": "general", "turns": turns}
        table = ae.render_table(ae.summarize([rec]))
        self.assertIn("| t | general | 1 | 1 | 100 |", table)


if __name__ == "__main__":
    unittest.main()
