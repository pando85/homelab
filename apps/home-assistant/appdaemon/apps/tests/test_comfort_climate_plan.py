"""The climate optimizer plan in the comfort controller (ADR-0008, Proposed): validation, the
three modes, the hard lock, shadow telemetry and the live path (reachable in tests only).

The plan documents are built by tests/climate_plan.py after the optimizer repository's
docs/climate-plan-contract.md. The lock `optimizer_live_allowed` is false in apps.yaml; only the
tests of the live path set it true, on their own copy of the arguments.
"""

import asyncio
import copy
import json
from datetime import date, datetime, timedelta, timezone

import pytest
import yaml
from aiohttp import web
from comfort_control import (
    MIN_ON,
    PLAN_MAX_BYTES,
    PLAN_REQUIRED_KEYS,
    ComfortControl,
    PlanSlot,
    parse_plan,
    plan_current,
    run_want,
)

from tests import comfort_sim as sim_module
from tests.climate_plan import CLIMATE_MODE, answer, hourly_server, make_plan
from tests.comfort_sim import Scenario, metrics, run_scenario
from tests.ha_template import render
from tests.test_comfort_control import APPS_DIR, TZ, dashboard_cards, local, make_app, room, tick_at

UTC = timezone.utc
# The unit tests run in the night phase (no early start, floor minimums only): a plan for 22:00Z (23:00
# local), run at 22:10Z, valid until 23:30Z. `tick(app, h, m)` is h hours and m minutes after 22:00Z.
T0 = datetime(2026, 11, 4, 22, 0, tzinfo=UTC)
HEAT_MODE = "input_select.heishamon_heatmode"
Z1_HELPER = "input_number.heishamon_heatshift_z1"
PLAN_SENSOR = "sensor.comfort_climate_plan_status"


def good(**kwargs):
    return make_plan(T0, **kwargs)


# --- Validation (pure) ---------------------------------------------------------------------------


def body(plan):
    return json.dumps(plan)


class TestParsePlan:
    def test_a_valid_plan_is_accepted(self):
        plan, reason = parse_plan(200, body(good(heating=lambda s: s.hour == 22 and s.minute < 30, water=30)))
        assert reason is None
        assert plan.plan_id == good(heating=lambda s: s.hour == 22 and s.minute < 30, water=30)["plan_id"]
        assert plan.valid_until == datetime(2026, 11, 4, 23, 30, tzinfo=UTC)
        assert plan.heating[:3] == (1, 1, 0) and plan.water_c[:3] == (30, 30, None) and plan.n == 96

    def test_the_builder_has_every_required_key_of_the_contract(self):
        assert set(PLAN_REQUIRED_KEYS) <= set(good())

    @pytest.mark.parametrize(
        "status,text,reason",
        [
            (404, json.dumps({"error": "no plan", "state": "mode_off"}), "not_served:mode_off"),
            (404, json.dumps({"state": "expired"}), "not_served:expired"),
            (404, "not json", "http_404"),
            (404, json.dumps(["x"]), "http_404"),
            (500, "", "http_500"),
            (503, body(good()), "http_503"),
            (None, "", "http_None"),
            (200, "", "bad_json"),
            (200, "{", "bad_json"),
            (200, "[]", "bad_json"),
            (200, "null", "bad_json"),
            (200, '"plan"', "bad_json"),
        ],
    )
    def test_absent_answers(self, status, text, reason):
        assert parse_plan(status, text) == (None, reason)

    @pytest.mark.parametrize(
        "mutation,reason",
        [
            ({"version": 2}, "version"),
            ({"version": "1"}, "version"),
            ({"version": 1.0}, "version"),
            ({"version": True}, "version"),
            ({"version": None}, "version"),
            ({"advisory": False}, "advisory"),
            ({"advisory": "true"}, "advisory"),
            ({"advisory": 1}, "advisory"),
            ({"advisory": None}, "advisory"),
            ({"status": "skipped", "slots": None, "reason": "observer warming up"}, "not_planned"),
            ({"status": "error", "slots": None, "reason": "solver failed"}, "not_planned"),
            ({"status": "PLANNED"}, "not_planned"),
            ({"plan_id": "749836f8fa4e1e9"}, "schema:plan_id"),
            ({"plan_id": "749836F8FA4E1E98"}, "schema:plan_id"),
            ({"plan_id": 7498368}, "schema:plan_id"),
            ({"generated_at": "2026-11-04T00:10:00"}, "schema:instant"),
            ({"generated_at": "2026-11-04T01:10:00+01:00"}, "schema:instant"),
            ({"valid_until": "soon"}, "schema:instant"),
            ({"valid_until": None}, "schema:instant"),
            ({"slots": None}, "schema:slots"),
            ({"slots": []}, "schema:slots"),
        ],
    )
    def test_top_level_violations(self, mutation, reason):
        assert parse_plan(200, body({**good(), **mutation})) == (None, reason)

    @pytest.mark.parametrize("key", PLAN_REQUIRED_KEYS)
    def test_every_required_key_must_be_present(self, key):
        plan = good()
        del plan[key]
        parsed, reason = parse_plan(200, body(plan))
        assert parsed is None and reason is not None

    @pytest.mark.parametrize(
        "slot_key,value",
        [
            ("start", "2026-11-04T00:00:00"),
            ("start", None),
            ("step_minutes", 30),
            ("step_minutes", "15"),
            ("n", 0),
            ("n", 193),
            ("n", "96"),
            ("n", 95),  # arrays are 96 long
            ("heating", [1] * 95),
            ("heating", [2] * 96),
            ("heating", [True] * 96),
            ("heating", "1" * 96),
            ("water_c", [36] * 96),
            ("water_c", [19] * 96),
            ("water_c", [30.5] * 96),
            ("water_c", ["30"] * 96),
            ("water_c", [30] * 95),
            ("water_c", None),
        ],
    )
    def test_slot_violations(self, slot_key, value):
        plan = good()
        plan["slots"][slot_key] = value
        parsed, reason = parse_plan(200, body(plan))
        assert parsed is None and reason.startswith("schema:"), reason

    def test_unknown_fields_are_ignored(self):
        plan = {**good(), "future": {"x": 1}}
        plan["slots"]["extra"] = [1]
        plan["inputs"]["more"] = True
        assert parse_plan(200, body(plan))[1] is None

    def test_a_replan_gets_another_id(self):
        assert good(heating=1)["plan_id"] != good(heating=0)["plan_id"]
        assert good()["plan_id"] == good()["plan_id"]


class TestPlanCurrent:
    def plan(self, **kwargs):
        return parse_plan(200, body(make_plan(T0, **kwargs)))[0]

    def test_the_slot_is_found_by_time(self):
        plan = self.plan(heating=lambda s: s.minute == 15, water=lambda s: 20 + s.minute // 15)
        assert plan_current(plan, T0 + timedelta(minutes=14, seconds=59))[0] == PlanSlot(0, T0, False, None)
        slot, reason = plan_current(plan, T0 + timedelta(minutes=15))
        assert reason is None and slot == PlanSlot(1, T0 + timedelta(minutes=15), True, 21)

    def test_local_time_gives_the_same_slot(self):
        plan = self.plan()
        assert plan_current(plan, local(2026, 11, 4, 23, 20)) == plan_current(plan, T0 + timedelta(minutes=20))

    def test_expired_at_valid_until_exactly(self):
        plan = self.plan()
        assert plan_current(plan, T0 + timedelta(minutes=89, seconds=59))[1] is None
        assert plan_current(plan, T0 + timedelta(minutes=90)) == (None, "expired")
        assert plan_current(plan, T0 + timedelta(days=1)) == (None, "expired")

    def test_slot_not_covered(self):
        plan = self.plan(n=4, ttl=timedelta(hours=3))  # covers one hour from T0
        assert plan_current(plan, T0 + timedelta(minutes=59))[1] is None
        assert plan_current(plan, T0 + timedelta(minutes=60)) == (None, "slot_not_covered")
        assert plan_current(plan, T0 - timedelta(minutes=1)) == (None, "slot_not_covered")


class TestRunWant:
    @pytest.mark.parametrize(
        "mode_on,cold,missing,satisfied,expected",
        [
            (False, [], [], False, "hold"),
            (False, ["bedroom"], [], False, "on"),
            (False, [], ["bedroom"], False, "on"),
            (False, [], [], True, "hold"),
            (True, [], [], True, "off"),
            (True, ["bedroom"], [], False, "hold"),
            (True, [], [], False, "hold"),
            (True, [], ["bedroom"], False, "hold"),
        ],
    )
    def test_the_rules(self, mode_on, cold, missing, satisfied, expected):
        assert run_want(mode_on, cold, missing, satisfied) == expected

    @pytest.mark.parametrize("mode_on", [False, True])
    def test_a_plan_with_heating_one_changes_nothing(self, mode_on):
        for cold, missing, satisfied in (([], [], True), (["a"], [], False), ([], ["a"], False), ([], [], False)):
            assert run_want(mode_on, cold, missing, satisfied, plan_heating=True) == run_want(mode_on, cold, missing, satisfied)

    def test_heating_zero_holds_heating_off(self):
        assert run_want(True, ["bedroom"], [], False, plan_heating=False) == "off"
        assert run_want(False, ["bedroom"], [], False, plan_heating=False) == "off"  # nothing to do: stays off

    def test_a_floor_forces_heating_whatever_the_plan_says(self):
        assert run_want(False, ["bedroom"], [], False, plan_heating=False, floor_forced=True) == "on"
        assert run_want(True, ["bedroom"], [], True, plan_heating=False, floor_forced=True) == "hold"  # on stays on
        assert run_want(True, [], [], True, plan_heating=True, floor_forced=True) == "hold"

    def test_a_silent_room_is_never_a_reason_to_stop_heating(self):
        assert run_want(False, [], ["bedroom"], False, plan_heating=False) == "on"
        assert run_want(True, [], ["bedroom"], False, plan_heating=False) == "hold"


# --- The real HTTP seam ----------------------------------------------------------------------------


@pytest.fixture()
def http_app():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ComfortControl, "__init__", lambda self: None)
        yield ComfortControl()


async def serve(handler):
    application = web.Application()
    application.router.add_route("*", "/climate/plan", handler)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/climate/plan"


class TestHttpGet:
    @pytest.mark.asyncio
    async def test_a_get_returns_status_and_text(self, http_app):
        seen = []

        async def handler(request):
            seen.append((request.method, request.headers.get("Accept")))
            return web.json_response(good())

        runner, url = await serve(handler)
        try:
            status, text = await http_app._http_get(url, 2)
        finally:
            await runner.cleanup()
        assert status == 200 and seen == [("GET", "application/json")]
        assert parse_plan(status, text)[1] is None

    @pytest.mark.asyncio
    async def test_a_404_is_returned_not_raised(self, http_app):
        async def handler(request):
            return web.json_response({"error": "no plan", "state": "mode_off"}, status=404)

        runner, url = await serve(handler)
        try:
            status, text = await http_app._http_get(url, 2)
        finally:
            await runner.cleanup()
        assert parse_plan(status, text) == (None, "not_served:mode_off")

    @pytest.mark.asyncio
    async def test_a_slow_answer_times_out(self, http_app):
        async def handler(request):
            await asyncio.sleep(2)
            return web.json_response(good())

        runner, url = await serve(handler)
        try:
            with pytest.raises(asyncio.TimeoutError):
                await http_app._http_get(url, 0.2)
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_an_oversized_body_is_not_a_plan(self, http_app):
        async def handler(request):
            return web.Response(text=" " * (PLAN_MAX_BYTES + 10), content_type="application/json")

        runner, url = await serve(handler)
        try:
            status, text = await http_app._http_get(url, 2)
        finally:
            await runner.cleanup()
        assert parse_plan(status, text) == (None, "bad_json")

    @pytest.mark.asyncio
    async def test_a_refused_connection_raises(self, http_app):
        with pytest.raises(Exception):
            await http_app._http_get("http://127.0.0.1:9/climate/plan", 1)


# --- The app: modes, fallbacks, shadow telemetry -----------------------------------------------------


class Optimizer:
    """The optimizer's /climate/plan as the app sees it: a response (or a callable of `now`)."""

    def __init__(self, app, response=None):
        self.app, self.requests, self.response = app, [], response if response is not None else answer(good())

    async def __call__(self, url, timeout):
        self.requests.append((self.app.clock[0], url, timeout))
        response = self.response(self.app.clock[0]) if callable(self.response) else self.response
        if isinstance(response, Exception):
            raise response
        return response


def plan_app(mode="shadow", allowed=False, response=None):
    """An app with the owner's select at `mode`, a stubbed optimizer and, if `allowed`, the lock open
    (on this test's own copy of the arguments, never apps.yaml)."""
    app = make_app()
    app.args = copy.deepcopy(app.args)
    app.args["optimizer_live_allowed"] = allowed
    if mode is not None:
        app.ha.put(CLIMATE_MODE, mode)
    app.optimizer = Optimizer(app, response)
    app._http_get = app.optimizer
    return app


def status(app):
    return app.ha.states[PLAN_SENSOR]


def attrs(app):
    return status(app)["attributes"]


def mode_writes(app):
    return [c[2] for c in app.ha.calls("set_state", HEAT_MODE)]


def z1_writes(app):
    return [c[2]["value"] for c in app.ha.calls("input_number/set_value", Z1_HELPER)]


async def tick(app, hour=0, minute=30):
    app.clock[0] = (T0 + timedelta(hours=hour, minutes=minute)).astimezone(TZ)
    await app._tick({})


class TestConfiguration:
    def test_the_lock_is_false_in_git(self):
        args = yaml.safe_load((APPS_DIR / "apps.yaml").read_text())["comfort_control"]
        assert args["optimizer_live_allowed"] is False
        assert args["climate_plan"]["mode_entity"] == CLIMATE_MODE
        assert args["climate_plan"]["status_entity"] == PLAN_SENSOR

    def test_the_plan_is_read_from_the_optimizers_service_by_get_only(self):
        args = yaml.safe_load((APPS_DIR / "apps.yaml").read_text())["comfort_control"]
        assert args["climate_plan"]["url"] == "http://battery-optimizer.battery-optimizer.svc:8080/climate/plan"
        source = (APPS_DIR / "comfort_control.py").read_text()
        assert "session.get(" in source
        for verb in ("session.post", "session.put", "session.patch", "session.delete", "requests.", "urllib"):
            assert verb not in source

    @pytest.mark.parametrize("value", [None, False, "true", "True", "yes", 1, 0, "", []])
    def test_only_a_real_true_opens_the_lock(self, value):
        app = make_app()
        app.args = {**app.args, "optimizer_live_allowed": value}
        assert app._live_effective("live") is (value is True)

    def test_a_missing_key_is_locked(self):
        app = make_app()
        app.args = {key: v for key, v in app.args.items() if key != "optimizer_live_allowed"}
        assert app._live_effective("live") is False
        assert app._live_effective("shadow") is False and app._live_effective("off") is False

    def test_the_select_is_never_written_by_the_app(self):
        source = (APPS_DIR / "comfort_control.py").read_text()
        assert "mode_entity" in source
        for line in source.splitlines():
            if "mode_entity" in line:
                assert "set_state" not in line and "call_service" not in line and "select_option" not in line


class TestModes:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", [None, "unavailable", "unknown", "", "Live", "OFF", "auto", "on"])
    async def test_a_missing_or_unrecognised_select_is_rules(self, raw):
        app = plan_app(mode=None)
        if raw is not None:
            app.ha.put(CLIMATE_MODE, raw)
        await tick(app)
        assert app.optimizer.requests == []  # nothing is fetched
        assert status(app)["state"] == "off" and attrs(app)["effective_mode"] == "off"
        assert attrs(app)["plan_usable"] is False and attrs(app)["fallback_reason"] == "mode_off"

    @pytest.mark.asyncio
    async def test_off_fetches_nothing_and_uses_nothing(self):
        app = plan_app(mode="off")
        await tick(app)
        assert app.optimizer.requests == []
        assert app.plan_slot is None and app.plan_control is False

    @pytest.mark.asyncio
    async def test_shadow_fetches_and_reports_without_control(self):
        app = plan_app(mode="shadow")
        await tick(app)
        assert len(app.optimizer.requests) == 1
        url = app.optimizer.requests[0][1]
        assert url == app.args["climate_plan"]["url"] and app.optimizer.requests[0][2] == 5
        assert status(app)["state"] == "shadow"
        assert attrs(app)["effective_mode"] == "shadow" and attrs(app)["plan_controls"] is False
        assert attrs(app)["live_locked"] is False

    @pytest.mark.asyncio
    async def test_live_with_the_lock_in_git_is_shadow_and_says_so(self):
        app = plan_app(mode="live", allowed=False)
        await tick(app)
        assert status(app)["state"] == "live_locked"
        assert attrs(app)["requested_mode"] == "live" and attrs(app)["effective_mode"] == "shadow"
        assert attrs(app)["live_locked"] is True and attrs(app)["live_allowed_in_git"] is False
        assert attrs(app)["plan_controls"] is False and app.plan_control is False
        assert "locked in git" in attrs(app)["status_text"]
        assert attrs(app)["plan_usable"] is True  # the plan is still read and compared

    @pytest.mark.asyncio
    async def test_live_with_the_lock_open_controls(self):
        app = plan_app(mode="live", allowed=True)
        await tick(app)
        assert status(app)["state"] == "live"
        assert attrs(app)["effective_mode"] == "live" and attrs(app)["live_locked"] is False
        assert attrs(app)["plan_controls"] is True

    @pytest.mark.asyncio
    async def test_the_select_changing_revokes_control_at_once(self):
        app = plan_app(mode="live", allowed=True)
        await tick(app)
        assert attrs(app)["plan_controls"] is True and app._plan_controls() is True
        app.ha.put(CLIMATE_MODE, "shadow")
        await app._on_plan_mode(CLIMATE_MODE, "", "live", "shadow", {})  # the state listener, no wait for the minute
        assert app.plan_control is False and app._plan_controls() is False
        await tick(app, 0, 31)
        assert attrs(app)["plan_controls"] is False and status(app)["state"] == "shadow"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("new", ["off", "shadow", "unavailable", "unknown", "garbage", None])
    async def test_every_other_value_revokes(self, new):
        app = plan_app(mode="live", allowed=True)
        await tick(app)
        await app._on_plan_mode(CLIMATE_MODE, "", "live", new, {})
        assert app._plan_controls() is False

    @pytest.mark.asyncio
    async def test_the_listener_never_widens_control(self):
        app = plan_app(mode="live", allowed=False)
        await tick(app)
        await app._on_plan_mode(CLIMATE_MODE, "", "shadow", "live", {})
        assert app._plan_controls() is False and app.plan_control is False

    @pytest.mark.asyncio
    async def test_the_listener_is_registered_on_the_select(self):
        app = make_app()
        listened = []

        async def listen_state(callback, entity, **kwargs):
            listened.append((callback.__name__, entity))

        async def run_every(*args, **kwargs):
            return None

        async def create_task(coroutine):
            coroutine.close()

        app.listen_state, app.run_every, app.create_task = listen_state, run_every, create_task
        await app.initialize()
        assert ("_on_plan_mode", CLIMATE_MODE) in listened


class TestFallbacks:
    CASES = {
        "fetch_failed": lambda now: ConnectionError("refused"),
        "timeout": lambda now: asyncio.TimeoutError(),
        "not_served:mode_off": lambda now: (404, json.dumps({"state": "mode_off"})),
        "http_404": lambda now: (404, "nope"),
        "http_500": lambda now: (500, ""),
        "bad_json": lambda now: (200, "{{{"),
        "version": lambda now: answer(good(version=2)),
        "advisory": lambda now: answer(good(advisory=False)),
        "not_planned": lambda now: answer(good(status="skipped", slots=None, reason="inputs incomplete")),
        "schema:slots.heating": lambda now: answer({**good(), "slots": {**good()["slots"], "heating": [3] * 96}}),
    }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", list(CASES))
    @pytest.mark.parametrize("mode,allowed", [("shadow", False), ("live", False), ("live", True)])
    async def test_an_unusable_plan_is_absent_and_the_rules_decide(self, name, mode, allowed):
        app = plan_app(mode=mode, allowed=allowed, response=self.CASES[name])
        room(app, "bedroom", current=21.0)  # the rules want heating
        await tick(app)
        assert attrs(app)["plan_usable"] is False and app.plan_slot is None and app.plan_control is False
        assert attrs(app)["fallback_reason"] == ("fetch_failed" if name == "timeout" else name)
        assert attrs(app)["plan_heating_now"] is None and attrs(app)["agree"] is None
        assert mode_writes(app) == ["Heat+DHW"]  # the rules, as without a plan
        assert attrs(app)["today_unusable"] == 1

    @pytest.mark.asyncio
    async def test_the_last_plan_is_never_kept_after_a_failed_fetch(self):
        app = plan_app(mode="live", allowed=True)
        await tick(app, 0, 30)
        assert attrs(app)["plan_usable"] is True
        app.optimizer.response = ConnectionError("down")
        await tick(app, 0, 31)  # not due yet: the plan fetched a minute ago is current and valid
        assert attrs(app)["plan_usable"] is True
        await tick(app, 0, 32)  # due: the fetch fails, the plan is gone
        assert attrs(app)["plan_usable"] is False and attrs(app)["fallback_reason"] == "fetch_failed"
        assert attrs(app)["plan_id"] is None and app.plan_control is False
        await tick(app, 0, 33)  # a failed fetch is retried every minute
        assert len(app.optimizer.requests) == 3
        app.optimizer.response = answer(good())
        await tick(app, 0, 34)
        assert attrs(app)["plan_usable"] is True

    @pytest.mark.asyncio
    async def test_expiry_is_checked_at_every_use_not_only_when_fetched(self):
        app = plan_app(mode="live", allowed=True)
        await tick(app, 1, 29)  # 01:29Z, the last minute of the plan
        assert attrs(app)["plan_usable"] is True and app.plan_control is True
        app.plan_next_fetch = local(2026, 11, 5, 23, 0)  # no fetch is due: the cached plan is judged on its own
        await tick(app, 1, 30)  # 01:30Z = valid_until
        assert len(app.optimizer.requests) == 1
        assert attrs(app)["plan_usable"] is False and attrs(app)["fallback_reason"] == "expired"
        assert app.plan_control is False
        assert attrs(app)["plan_age_min"] == 80 and attrs(app)["plan_expires_in_min"] == 0

    @pytest.mark.asyncio
    async def test_a_slot_that_does_not_cover_now_is_absent(self):
        plan = good(n=4, ttl=timedelta(hours=3))  # covers 00:00Z-01:00Z only
        app = plan_app(mode="live", allowed=True, response=answer(plan))
        await tick(app, 1, 15)  # 01:15Z
        assert attrs(app)["plan_usable"] is False and attrs(app)["fallback_reason"] == "slot_not_covered"
        assert app.plan_control is False

    @pytest.mark.asyncio
    async def test_a_plan_from_the_future_is_absent_until_its_slot(self):
        app = plan_app(mode="shadow", response=answer(make_plan(T0 + timedelta(hours=2), ttl=timedelta(hours=3))))
        await tick(app, 0, 30)
        assert attrs(app)["fallback_reason"] == "slot_not_covered"

    @pytest.mark.asyncio
    async def test_a_replan_replaces_the_previous_plan_entirely(self):
        app = plan_app(mode="live", allowed=True, response=answer(good(heating=0)))
        await tick(app, 0, 30)
        assert attrs(app)["plan_heating_now"] is False
        app.optimizer.response = answer(make_plan(T0, heating=1, water=30))
        await tick(app, 0, 32)
        assert attrs(app)["plan_heating_now"] is True and attrs(app)["plan_water_c_now"] == 30
        assert attrs(app)["today_plans"] == 2


class TestPlanIdHandling:
    """ADR-0008 decision 6 and contract rule 3: apply a plan once per plan_id."""

    @pytest.mark.asyncio
    async def test_the_same_plan_keeps_driving_until_replaced_or_expired(self):
        app = plan_app(mode="live", allowed=True)
        for minute in (30, 32, 34, 36):
            await tick(app, 0, minute)
            assert app.plan_control is True and app.plan_blocked is None
        assert app.plan_live_applied == good()["plan_id"]
        assert sum("applied (live)" in line for line in app.logs) == 1

    @pytest.mark.asyncio
    async def test_a_superseded_plan_is_not_applied_again(self):
        old, new = good(heating=0), make_plan(T0, heating=1, water=30)
        app = plan_app(mode="live", allowed=True, response=answer(old))
        await tick(app, 0, 30)
        app.optimizer.response = answer(new)
        await tick(app, 0, 32)
        assert app.plan_live_applied == new["plan_id"] and app.plan_control is True
        app.optimizer.response = answer(old)  # a stale answer after the replan
        await tick(app, 0, 34)
        assert app.plan_control is False and attrs(app)["control_blocked"] == "plan_id_already_applied"
        assert attrs(app)["plan_usable"] is True  # still shown (telemetry), but not applied

    @pytest.mark.asyncio
    async def test_leaving_live_revokes_and_the_same_plan_is_not_re_applied(self):
        app = plan_app(mode="live", allowed=True)
        await tick(app, 0, 30)
        assert app.plan_control is True
        app.ha.put(CLIMATE_MODE, "shadow")
        await tick(app, 0, 31)
        assert app.plan_control is False and any("revoked" in line for line in app.logs)
        app.ha.put(CLIMATE_MODE, "live")
        await tick(app, 0, 32)
        assert app.plan_control is False and attrs(app)["control_blocked"] == "plan_id_already_applied"
        assert status(app)["state"] == "live"  # live is effective, but this plan is spent
        # The next plan (new id) drives again.
        app.optimizer.response = answer(make_plan(T0 + timedelta(hours=1), heating=1, water=31))
        await tick(app, 1, 34)
        assert app.plan_control is True

    @pytest.mark.asyncio
    async def test_shadow_never_marks_a_plan_applied(self):
        app = plan_app(mode="shadow")
        await tick(app, 0, 30)
        assert app.plan_live_applied is None and not any("applied" in line for line in app.logs)
        app.ha.put(CLIMATE_MODE, "live")  # lock closed: still nothing applied
        await tick(app, 0, 31)
        assert app.plan_live_applied is None


class TestShadowTelemetry:
    @pytest.mark.asyncio
    async def test_the_status_attributes(self):
        app = plan_app(mode="live", allowed=False, response=answer(good(heating=1, water=33)))
        app.ha.put("input_select.heishamon_heatmode", "Heat+DHW")
        room(app, "bedroom", current=21.0)  # cold: rules heat
        app.ha.put(app.args["sensors"]["outdoor"], "5")  # curve 28.5 -> request 29
        await tick(app, 0, 30)
        a = attrs(app)
        plan = good(heating=1, water=33)
        assert a["plan_id"] == plan["plan_id"]
        assert a["plan_generated_at"] == "2026-11-04T22:10:00+00:00" and a["plan_valid_until"] == "2026-11-04T23:30:00+00:00"
        assert a["plan_age_min"] == 20 and a["plan_expires_in_min"] == 60
        assert a["slot_start"] == "2026-11-04T22:30:00+00:00"
        assert (a["plan_heating_now"], a["plan_water_c_now"]) == (True, 33)
        assert (a["rules_heating_now"], a["rules_water_c_now"]) == (True, 29)
        assert a["agree_heating"] is True and a["agree_water"] is False and a["agree"] is False
        assert a["requested_mode"] == "live" and a["effective_mode"] == "shadow" and a["live_locked"] is True
        assert a["plan_usable"] is True and a["fallback_reason"] is None
        assert a["today"] == "2026-11-04" and a["today_samples"] == 1 and a["today_agree"] == 0
        assert a["today_water_disagree"] == 1 and a["today_heating_disagree"] == 0

    @pytest.mark.asyncio
    async def test_rules_and_plan_that_agree(self):
        app = plan_app(mode="shadow", response=answer(good(heating=0)))
        await tick(app)  # rooms at setpoint, heating off: rules want no heating either
        a = attrs(app)
        assert (a["plan_heating_now"], a["rules_heating_now"], a["agree"]) == (False, False, True)
        assert a["agree_water"] is None  # no plan water in a heating-off slot
        assert a["today_agree"] == 1 and a["today_agree_pct"] == 100

    @pytest.mark.asyncio
    async def test_what_the_live_path_would_do_is_reported(self):
        app = plan_app(mode="shadow", response=answer(good(heating=0)))
        app.ha.put("input_select.heishamon_heatmode", "Heat+DHW")
        room(app, "bedroom", current=21.0)  # cold, above its floor minus the hysteresis (20.7)
        await tick(app)
        a = attrs(app)
        assert a["rules_heating_now"] is True and a["plan_heating_now"] is False and a["agree"] is False
        assert a["plan_effect_heating_now"] is False and a["floor_forced"] == []
        assert a["today_would_differ"] == 1 and a["today_heating_disagree"] == 1
        room(app, "bedroom", current=20.5)  # below the floor minus the hysteresis: the floor wins
        await tick(app, 0, 31)
        a = attrs(app)
        assert a["floor_forced"] == ["bedroom"] and a["plan_effect_heating_now"] is True
        assert a["today_would_differ"] == 1  # unchanged: live would not differ now

    @pytest.mark.asyncio
    async def test_one_sample_per_minute(self):
        app = plan_app(mode="shadow")
        await tick(app, 0, 30)
        await tick(app, 0, 30)
        assert attrs(app)["today_samples"] == 1
        await tick(app, 0, 31)
        assert attrs(app)["today_samples"] == 2

    @pytest.mark.asyncio
    async def test_counters_reset_at_local_midnight_and_survive_a_restart(self):
        app = plan_app(mode="shadow", response=hourly_server())
        for minute in (30, 31, 32):
            await tick(app, 0, minute)
        assert attrs(app)["today_samples"] == 3 and attrs(app)["today"] == "2026-11-04"
        restarted = plan_app(mode="shadow", response=hourly_server())
        restarted.ha = app.ha  # the sensor survives an AppDaemon restart
        for name in ("get_state", "set_state", "call_service", "get_history", "sleep"):
            setattr(restarted, name, getattr(app.ha, name))
        await restarted._load_plan_status()
        assert restarted.plan_today["samples"] == 3
        await tick_at(app, 2026, 11, 5, 0, 5)
        assert attrs(app)["today"] == "2026-11-05" and attrs(app)["today_samples"] == 1

    @pytest.mark.asyncio
    async def test_a_restart_on_another_day_starts_from_zero(self):
        app = plan_app(mode="shadow")
        app.ha.put(PLAN_SENSOR, "shadow", today="2026-11-03", today_samples=50, today_agree=40)
        await app._load_plan_status()
        assert app.plan_today["samples"] == 0

    @pytest.mark.asyncio
    async def test_garbage_in_the_restored_counters_is_zero(self):
        app = plan_app(mode="shadow")
        app.ha.put(PLAN_SENSOR, "shadow", today="2026-11-04", today_samples="x", today_agree=True, today_plans=-3)
        await app._load_plan_status()
        assert app.plan_today["samples"] == 0 and app.plan_today["agree"] == 0 and app.plan_today["plans"] == 0

    @pytest.mark.asyncio
    async def test_log_lines_say_what_the_plan_and_the_rules_do(self):
        app = plan_app(mode="live", allowed=False, response=answer(good(heating=1, water=33)))
        await tick(app, 0, 30)
        lines = [line for line in app.logs if line.startswith("Climate plan")]
        assert any("received" in line and good(heating=1, water=33)["plan_id"] in line for line in lines)
        assert any("[live_locked]" in line and "plan heating=True water=33" in line and "agree=" in line for line in lines)
        app.optimizer.response = ConnectionError("down")
        await tick(app, 0, 32)
        assert any("not usable (fetch_failed): rules decide" in line for line in app.logs)

    @pytest.mark.asyncio
    async def test_the_sensor_is_written_only_on_change(self):
        app = plan_app(mode="off")
        await tick(app, 0, 30)
        await tick(app, 0, 31)
        assert len(app.ha.calls("set_state", PLAN_SENSOR)) == 1  # off: constant, written once

    @pytest.mark.asyncio
    async def test_a_disabled_controller_uses_no_plan(self):
        app = plan_app(mode="live", allowed=True)
        app.ha.put(app.args["helpers"]["enable"], "off")
        await tick(app)
        assert app.optimizer.requests == []
        assert attrs(app)["plan_controls"] is False and attrs(app)["fallback_reason"] == "controller_disabled"

    @pytest.mark.asyncio
    async def test_a_failing_plan_cycle_cannot_stop_the_control_tick(self):
        app = plan_app(mode="live", allowed=True)

        async def boom(url, timeout):
            raise RuntimeError("boom")

        async def broken(self=None):
            raise RuntimeError("select unreadable")

        app._http_get = boom
        app._plan_requested_mode = broken
        room(app, "bedroom", current=21.0)
        await tick(app)
        assert mode_writes(app) == ["Heat+DHW"]  # the rules ran
        assert app.plan_control is False and attrs(app)["fallback_reason"] == "internal_error"


# --- The live path (the lock forced open, in tests only) ------------------------------------------------


def live_app(response=None, mode="live"):
    app = plan_app(mode=mode, allowed=True, response=response)
    app.ha.put("input_select.heishamon_heatmode", "Heat+DHW")  # the heat pump runs
    app.last_mode_change = local(2026, 11, 4, 18, 0)  # long enough ago for the minimum on time
    return app


class TestLiveRunLogic:
    @pytest.mark.asyncio
    async def test_heating_zero_stops_heating_for_a_cold_room_above_its_floor(self):
        app = live_app(answer(good(heating=0)))
        room(app, "bedroom", current=21.0)  # the rules would keep heating (below 21.5), floor minus hysteresis is 20.7
        await tick(app)
        assert mode_writes(app) == ["DHW"]
        assert any("plan: heating not permitted in the slot of 22:30 UTC" in line for line in app.logs)

    @pytest.mark.asyncio
    async def test_the_same_situation_with_the_lock_in_git_leaves_the_rules_alone(self):
        for mode, allowed in (("shadow", False), ("live", False), ("off", False), ("shadow", True)):
            app = plan_app(mode=mode, allowed=allowed, response=answer(good(heating=0)))
            app.ha.put("input_select.heishamon_heatmode", "Heat+DHW")
            app.last_mode_change = local(2026, 11, 4, 18, 0)
            room(app, "bedroom", current=21.0)
            await tick(app)
            assert mode_writes(app) == [], (mode, allowed)

    @pytest.mark.asyncio
    async def test_heating_zero_keeps_a_stopped_pump_stopped(self):
        app = live_app(answer(good(heating=0)))
        app.ha.put(HEAT_MODE, "DHW")
        room(app, "bedroom", current=20.9)  # cold, but above the floor minus the hysteresis
        await tick(app)
        assert mode_writes(app) == []
        assert app.ha.states[HEAT_MODE]["state"] == "DHW"

    @pytest.mark.asyncio
    async def test_a_room_below_its_floor_forces_heating_whatever_the_plan_says(self):
        app = live_app(answer(good(heating=0)))
        app.ha.put(HEAT_MODE, "DHW")
        app.last_mode_change = local(2026, 11, 4, 18, 0)
        room(app, "bedroom", current=20.6)  # floor 21 minus hysteresis 0.3 = 20.7
        await tick(app)
        assert mode_writes(app) == ["Heat+DHW"]
        assert any("comfort floor beats the plan: bedroom" in line for line in app.logs)
        assert attrs(app)["floor_forced"] == ["bedroom"] and attrs(app)["plan_controls"] is True

    @pytest.mark.asyncio
    async def test_a_floor_forced_room_keeps_a_running_pump_on_even_when_the_thermostats_are_satisfied(self):
        app = live_app(answer(good(heating=0)))
        room(app, "bedroom", current=20.6, target=19.0)  # the owner lowered the thermostat below the floor
        for entity in ("hall_kitchen", "office_a", "office_t"):
            room(app, entity, current=24.5)
        await tick(app)
        assert mode_writes(app) == []

    @pytest.mark.asyncio
    async def test_the_day_minimum_counts_inside_the_day_window(self):
        app = live_app(answer(make_plan(datetime(2026, 11, 4, 11, 0, tzinfo=UTC), heating=0)))
        app.ha.put(HEAT_MODE, "DHW")
        app.last_mode_change = local(2026, 11, 4, 8, 0)
        # 12:30 local is 11:30Z, inside the plan; the day window is 12:00-22:00 local, the hall's day minimum 23.5.
        room(app, "hall_kitchen", current=23.1, target=24.0)  # 23.5 - 0.3 = 23.2 > 23.1
        for entity in ("bedroom", "office_a", "office_t"):
            room(app, entity, current=21.6)
        app.clock[0] = local(2026, 11, 4, 12, 30)
        await app._tick({})
        assert attrs(app)["floor_forced"] == ["hall_kitchen"]
        assert mode_writes(app) == ["Heat+DHW"]

    @pytest.mark.asyncio
    async def test_away_uses_the_floor_only(self):
        app = live_app(answer(make_plan(datetime(2026, 11, 4, 11, 0, tzinfo=UTC), heating=0)))
        app.ha.put(app.args["helpers"]["away"], "on")
        app.ha.put(HEAT_MODE, "DHW")
        room(app, "hall_kitchen", current=22.0, target=21.5)
        app.clock[0] = local(2026, 11, 4, 12, 30)
        await app._tick({})
        assert attrs(app)["floor_forced"] == []  # day minimum 23.5 does not apply while away
        assert mode_writes(app) == []

    @pytest.mark.asyncio
    async def test_a_silent_room_keeps_heating_on_despite_the_plan(self):
        app = live_app(answer(good(heating=0)))
        app.ha.put(app.args["rooms"]["bedroom"], "unavailable", current_temperature="unavailable")
        await tick(app)
        assert mode_writes(app) == []  # still Heat+DHW
        assert app.ha.states[HEAT_MODE]["state"] == "Heat+DHW"

    @pytest.mark.asyncio
    async def test_minimum_on_time_holds_even_for_the_plan(self):
        app = live_app(answer(good(heating=0)))
        app.last_mode_change = local(2026, 11, 4, 23, 30) - MIN_ON + timedelta(minutes=1)
        room(app, "bedroom", current=21.0)
        await tick(app)
        assert mode_writes(app) == []
        await tick(app, 0, 31)
        assert mode_writes(app) == ["DHW"]

    @pytest.mark.asyncio
    async def test_minimum_off_time_holds_even_for_a_floor(self):
        app = live_app(answer(good(heating=0)))
        app.ha.put(HEAT_MODE, "DHW")
        app.last_mode_change = local(2026, 11, 4, 23, 30) - timedelta(minutes=19)
        room(app, "bedroom", current=20.0)
        await tick(app)
        assert mode_writes(app) == []
        await tick(app, 0, 31)
        assert mode_writes(app) == ["Heat+DHW"]

    @pytest.mark.asyncio
    async def test_heating_one_changes_nothing_the_thermostats_decide(self):
        app = live_app(answer(good(heating=1, water=30)))
        app.ha.put(HEAT_MODE, "DHW")
        await tick(app)  # rooms satisfied: no heating although the plan permits it
        assert mode_writes(app) == []
        room(app, "bedroom", current=21.0)
        await tick(app, 0, 31)
        assert mode_writes(app) == ["Heat+DHW"]
        for entity in app.args["rooms"].values():
            room(app, entity.removeprefix("climate.thermostat_"), current=22.0)
        app.last_mode_change = local(2026, 11, 4, 18, 0)
        await tick(app, 0, 32)
        assert mode_writes(app) == ["Heat+DHW", "DHW"]  # satisfied: off, as the rules say

    @pytest.mark.asyncio
    async def test_revoking_returns_to_the_rules_at_once(self):
        app = live_app(answer(good(heating=0)))
        room(app, "bedroom", current=21.0)
        app.ha.put(CLIMATE_MODE, "shadow")
        await tick(app)
        assert mode_writes(app) == []  # the rules keep heating (a cold room)

    @pytest.mark.asyncio
    async def test_the_run_step_with_no_plan_is_the_rules(self):
        app = live_app(response=ConnectionError("down"))
        room(app, "bedroom", current=21.0)
        await tick(app)
        assert mode_writes(app) == []  # on and cold: nothing to do
        app.ha.put(HEAT_MODE, "DHW")
        await tick(app, 0, 31)
        assert mode_writes(app) == ["Heat+DHW"]


class TestLiveWater:
    @pytest.mark.asyncio
    async def test_the_plan_water_replaces_the_curve_target(self):
        app = live_app(answer(good(heating=1, water=33)))
        app.ha.put(app.args["sensors"]["outdoor"], "5")  # curve 28.5
        await tick(app)
        assert z1_writes(app) == [33]
        assert "plan water_c 33 (curve 28.5)" in " ".join(app.logs)

    @pytest.mark.asyncio
    async def test_the_same_slot_without_control_writes_the_curve(self):
        for mode, allowed in (("shadow", False), ("live", False)):
            app = plan_app(mode=mode, allowed=allowed, response=answer(good(heating=1, water=33)))
            app.ha.put(app.args["sensors"]["outdoor"], "0")
            await tick(app)
            assert z1_writes(app) == [30], mode  # the curve (30.25 rounds to 30), never 33

    @pytest.mark.asyncio
    async def test_the_water_write_rules_still_apply(self):
        app = live_app(answer(good(heating=1, water=lambda s: 20 + (s.minute // 15) * 2)))  # 20, 22, 24, 26 per slot
        app.ha.put(app.args["sensors"]["z1_request"], "28")
        await tick(app, 0, 0)  # 00:00Z, slot 0: 20 vs 28
        assert z1_writes(app) == [20]
        app.ha.put(app.args["sensors"]["z1_request"], "20")
        await tick(app, 0, 15)  # slot 1: 22, but only 15 minutes after the last write
        assert z1_writes(app) == [20, 22]
        app.ha.put(app.args["sensors"]["z1_request"], "22")
        await tick(app, 0, 20)  # same slot: no change
        await tick(app, 0, 25)
        assert z1_writes(app) == [20, 22]
        app.ha.put(app.args["sensors"]["z1_request"], "21.5")
        await tick(app, 0, 40)  # slot 2: 24 vs 21.5 (2.5 K): written
        assert z1_writes(app)[-1] == 24

    @pytest.mark.asyncio
    async def test_a_change_under_one_kelvin_is_not_written(self):
        app = live_app(answer(good(heating=1, water=30)))
        app.ha.put(app.args["sensors"]["z1_request"], "29.4")
        await tick(app)
        assert z1_writes(app) == []

    @pytest.mark.asyncio
    async def test_rate_limit_is_one_write_per_fifteen_minutes(self):
        app = live_app(answer(good(heating=1, water=lambda s: 33 if s.minute < 30 else 21)))
        app.ha.put(app.args["sensors"]["z1_request"], "28")
        await tick(app, 0, 15)  # 00:15Z: 33
        assert z1_writes(app) == [33]
        app.ha.put(app.args["sensors"]["z1_request"], "33")
        await tick(app, 0, 30)  # 00:30Z: 21, but only 15 minutes later: due exactly now
        assert z1_writes(app) == [33, 21]
        app.ha.put(app.args["sensors"]["z1_request"], "21")
        app.last_water_write = local(2026, 11, 4, 23, 30)
        app.optimizer.response = answer(good(heating=1, water=34))
        app.plan_next_fetch = None
        await tick(app, 0, 40)
        assert z1_writes(app) == [33, 21]  # 10 minutes after the last write

    @pytest.mark.asyncio
    async def test_a_heating_off_slot_has_no_plan_water_so_the_curve_stays(self):
        app = live_app(answer(good(heating=0)))
        app.ha.put(app.args["sensors"]["outdoor"], "0")
        await tick(app)
        assert z1_writes(app) == [30]

    @pytest.mark.asyncio
    async def test_no_write_outside_direct_mode_even_for_the_plan(self):
        app = live_app(answer(good(heating=1, water=33)))
        app.ha.put(app.args["sensors"]["heating_mode"], "0")
        await tick(app)
        assert z1_writes(app) == []

    @pytest.mark.asyncio
    async def test_the_water_dry_run_helper_still_wins(self):
        app = live_app(answer(good(heating=1, water=33)))
        app.ha.put(app.args["helpers"]["water_dry_run"], "on")
        await tick(app)
        assert z1_writes(app) == []

    @pytest.mark.asyncio
    async def test_a_water_value_out_of_range_never_reaches_the_controller(self):
        for value in (36, 19, 99):
            plan = good()
            plan["slots"]["water_c"] = [value] * 96
            app = live_app(answer(plan))
            app.ha.put(app.args["sensors"]["outdoor"], "0")
            await tick(app)
            assert app.plan_slot is None and z1_writes(app) == [30]  # the plan is absent: the curve

    @pytest.mark.asyncio
    async def test_a_heating_slot_without_water_uses_the_curve(self):
        plan = good(heating=1, water=33)
        plan["slots"]["water_c"] = [None] * 96
        app = live_app(answer(plan))
        app.ha.put(app.args["sensors"]["outdoor"], "0")
        await tick(app)
        assert z1_writes(app) == [30]


class TestLiveNeverBreaksTheFixedLimits:
    @pytest.mark.asyncio
    async def test_no_heater_boost_sterilisation_or_tank_change_comes_from_the_plan(self):
        app = live_app(answer(good(heating=1, water=35)))
        for minute in range(30, 60):
            await tick(app, 0, minute)
        entities = {c[1] for c in app.ha.services if c[0] != "set_state"}
        assert entities <= {Z1_HELPER, "automation.set_heatmode", "input_text.comfort_dhw_plan", None}, entities
        assert not app.ha.calls("switch/turn_on") and not app.ha.calls("input_number/set_value", app.args["commands"]["tank_target"])


# --- Whole simulated days (the plant, Home Assistant and the controller in a loop) ------------------------

PLAN_START = date(2026, 11, 2)
LONG = pytest.mark.long

# A plan that disagrees with the rules all day: heating on in odd hours only, a different water level each hour.
def busy(slot_start):
    return slot_start.hour % 2


def level(slot_start):
    return 20 + slot_start.hour % 16


_SIMS = {}


def simulated(name, **kwargs):
    """Five cold simulated days (two of warm-up), cached per name."""
    if name not in _SIMS:
        _SIMS[name] = run_scenario(Scenario(name, PLAN_START, 5, "cold", "double_peak", seed=3, **kwargs))
    return _SIMS[name]


def writes(sim, with_plan_sensor=False):
    """Every call of the controller to Home Assistant: (time, caller, service, entity, data)."""
    return [c for c in sim.ha.services if with_plan_sensor or c[3] != PLAN_SENSOR]


def baseline():
    return simulated("plan_baseline")


@LONG
class TestNoWritePathFromThePlanWhileLocked:
    """With the lock false the plan is telemetry: the controller's calls to Home Assistant are
    identical, call for call, to the run with no plan at all, over whole simulated days."""

    @pytest.mark.parametrize("plan", ["busy", "never_heat"])
    def test_shadow_writes_exactly_what_the_rules_write(self, plan):
        heating = busy if plan == "busy" else 0
        shadow = simulated(f"plan_shadow_{plan}", climate_mode="shadow", climate_plan=hourly_server(heating=heating, water=level))
        assert len(shadow.plan_requests) > 3000  # the plan really was fetched every two minutes
        assert writes(shadow) == writes(baseline())
        assert writes(shadow)  # and the rules really wrote (a vacuous equality would pass on []))
        assert attrs_of(shadow)["today_samples"] > 500 and attrs_of(shadow)["plan_usable"] is True

    @pytest.mark.parametrize("plan", ["busy", "never_heat"])
    def test_live_with_the_lock_in_git_writes_exactly_what_the_rules_write(self, plan):
        heating = busy if plan == "busy" else 0
        locked = simulated(f"plan_locked_{plan}", climate_mode="live", climate_plan=hourly_server(heating=heating, water=level))
        assert len(locked.plan_requests) > 3000
        assert writes(locked) == writes(baseline())
        state = locked.ha.states[PLAN_SENSOR]
        assert state["state"] == "live_locked" and "locked in git" in state["attributes"]["status_text"]
        assert state["attributes"]["plan_controls"] is False and state["attributes"]["effective_mode"] == "shadow"
        # The plan disagreed with the rules (so a live plan would have acted), and still nothing was written.
        assert state["attributes"]["today_would_differ"] > 100 and state["attributes"]["today_heating_disagree"] > 100

    def test_the_owner_flipping_the_select_through_every_mode_changes_nothing(self):
        events = [
            ((2026, 11, 2, 6, 0), "climate_mode", {"value": "shadow"}),
            ((2026, 11, 3, 6, 0), "climate_mode", {"value": "live"}),
            ((2026, 11, 4, 6, 0), "climate_mode", {"value": "off"}),
            ((2026, 11, 4, 18, 0), "climate_mode", {"value": "live"}),
            ((2026, 11, 5, 3, 0), "climate_mode", {"value": "unavailable"}),
            ((2026, 11, 5, 9, 0), "climate_mode", {"value": "live"}),
        ]
        flipped = simulated("plan_flipped", climate_mode="off", climate_plan=hourly_server(heating=0, water=level), events=events)
        assert len(flipped.plan_requests) > 1500
        assert writes(flipped) == writes(baseline())

    def test_an_unreachable_or_broken_optimizer_changes_nothing(self):
        for name, server in (("plan_down", None), ("plan_garbage", lambda now: (200, "<html>"))):
            sim = simulated(name, climate_mode="live", climate_plan=server)
            assert writes(sim) == writes(baseline())
            attributes = sim.ha.states[PLAN_SENSOR]["attributes"]
            assert attributes["plan_usable"] is False and attributes["today_unusable"] > 500

    def test_the_select_is_never_written_by_the_app(self):
        sim = simulated("plan_locked_busy", climate_mode="live", climate_plan=hourly_server(heating=busy, water=level))
        assert not [c for c in sim.ha.services if c[3] == CLIMATE_MODE]


def attrs_of(sim):
    return sim.ha.states[PLAN_SENSOR]["attributes"]


@LONG
class TestUnlockedLiveInSimulation:
    """The live path, reachable only with the lock forced open in the test."""

    def unlocked(self, name="plan_unlocked_never", heating=0, **kwargs):
        return simulated(name, climate_mode="live", climate_live_allowed=True,
                         climate_plan=hourly_server(heating=heating, water=level), **kwargs)

    def test_the_plan_acts_and_comfort_floors_still_win(self):
        live = self.unlocked()
        assert writes(live) != writes(baseline())  # the plan changed what the controller does
        assert attrs_of(live)["plan_controls"] is True and attrs_of(live)["effective_mode"] == "live"
        result = metrics(live)
        # A plan that never heats: every room is still held at its floor minus the hysteresis (0.3 K),
        # with a little overshoot from the compressor minimum off time.
        assert result["min_room"] >= 20.5, result
        reasons = " ".join(entry[3] for entry in live.logs if entry[1] == "comfort_control")
        assert "comfort floor beats the plan" in reasons and "plan: heating not permitted" in reasons

    def test_a_busy_plan_keeps_the_fixed_limits(self):
        live = self.unlocked("plan_unlocked_busy", heating=busy)
        result = metrics(live)
        assert result["min_room"] >= 20.5 and result["max_tank"] <= 50 and result["max_tank_target"] <= 50
        assert result["starts_per_day"] <= 12
        for entity, value in [(c[3], c[4].get("value")) for c in live.ha.services if c[2] == "input_number/set_value"]:
            if entity == Z1_HELPER:
                assert 20 <= value <= 35 and float(value) == int(value)
        durations = sim_module.mode_durations(live)
        assert durations and min(minutes for _, minutes in durations) >= 19  # minimum on 30 / off 20 (to the tick)
        # No heater, boost or sterilisation: the same entities as the rules touch.
        assert {c[3] for c in writes(live)} <= {c[3] for c in writes(baseline())} | {Z1_HELPER, HEAT_MODE}

    def test_the_select_revokes_plan_control_immediately(self):
        events = [((2026, 11, 4, 12, 0), "climate_mode", {"value": "shadow"})]
        live = self.unlocked("plan_unlocked_revoked", heating=busy, events=events)
        revoked_at = local(2026, 11, 4, 12, 0)

        def logged(text):
            return [entry[0] for entry in live.logs if text in entry[3]]

        for text in ("plan: heating not permitted", "plan water_c"):
            before, after = logged(text), [at for at in logged(text) if at > revoked_at]
            assert any(at < revoked_at for at in before), text  # the live plan did act ...
            assert after == [], text  # ... and stopped acting the moment the select left live
        assert any("revoked" in entry[3] for entry in live.logs)
        assert attrs_of(live)["plan_controls"] is False and attrs_of(live)["effective_mode"] == "shadow"


# --- The Comfort dashboard section ------------------------------------------------------------------------


class TestClimateOptimizerDashboardSection:
    def markdown(self):
        (card,) = [c for c in dashboard_cards() if c["type"] == "markdown" and PLAN_SENSOR in c["content"]]
        return card["content"]

    def render(self, attributes):
        store = {} if attributes is None else {PLAN_SENSOR: {"state": "shadow", "attributes": attributes}}
        return render(self.markdown(), store, local(2026, 11, 4, 23, 30))

    def test_the_section_has_the_mode_select_and_the_status(self):
        cards = list(dashboard_cards())
        (select,) = [c for c in cards if c.get("entity") == CLIMATE_MODE]
        assert select["type"] == "tile" and select["features"] == [{"type": "select-options"}]
        assert any(c.get("entity") == PLAN_SENSOR and c["type"] == "tile" for c in cards)
        assert any("locked in git" in c.get("content", "") for c in cards if c["type"] == "markdown")

    def test_the_section_follows_the_comfort_control_section(self):
        import json

        sections = json.loads((APPS_DIR.parents[1] / "lovelace" / "dashboard-comfort.json").read_text())["config"]["views"][0]["sections"]
        headings = [next(c["heading"] for c in section["cards"] if c["type"] == "heading") for section in sections]
        assert headings[:3] == ["Comfort control", "Climate optimizer (shadow)", "Hall / Kitchen"]

    def test_not_available_before_the_first_publication(self):
        assert "not available yet" in self.render(None)

    def test_locked_live_with_a_usable_plan(self):
        text = self.render(
            {
                "requested_mode": "live", "effective_mode": "shadow", "live_locked": True, "plan_controls": False,
                "plan_usable": True, "plan_id": "749836f8fa4e1e98", "plan_age_min": 20, "plan_expires_in_min": 60,
                "plan_heating_now": True, "rules_heating_now": False, "plan_water_c_now": 33, "rules_water_c_now": 30,
                "agree": False, "floor_forced": [], "today_agree_pct": 71, "today_samples": 600, "today_heating_disagree": 100,
                "today_water_disagree": 70, "today_would_differ": 40, "today_unusable": 3,
            }
        )
        assert "requested **live**, effective **shadow**" in text
        assert "Live requested, locked in git" in text
        assert "749836f8fa4e1e98" in text and "20 min old, expires in 60 min" in text
        assert "heating on vs off" in text and "water 33 vs 30" in text and "agree: no" in text
        assert "agreement 71 % of 600 minutes" in text and "live would have acted differently 40" in text
        assert "driving" not in text

    def test_a_floor_override_and_a_driving_plan_are_shown(self):
        text = self.render(
            {
                "requested_mode": "live", "effective_mode": "live", "live_locked": False, "plan_controls": True,
                "plan_usable": True, "plan_id": "a" * 16, "plan_age_min": 5, "plan_expires_in_min": 75,
                "plan_heating_now": False, "rules_heating_now": True, "plan_water_c_now": None, "rules_water_c_now": 31,
                "agree": False, "floor_forced": ["bedroom", "office_t"],
            }
        )
        assert "driving the run decision" in text and "locked in git" not in text
        assert "A comfort floor overrides the plan in: bedroom, office_t" in text and "water - vs 31" in text

    def test_no_usable_plan_names_the_reason(self):
        text = self.render({"requested_mode": "shadow", "effective_mode": "shadow", "plan_usable": False, "fallback_reason": "expired", "control_blocked": None})
        assert "No usable plan** (expired): the rules decide" in text
        text = self.render({"requested_mode": "off", "effective_mode": "off", "plan_usable": False, "fallback_reason": "mode_off"})
        assert "mode off" in text and "No usable plan" not in text

    def test_a_spent_plan_is_said(self):
        text = self.render({"requested_mode": "live", "effective_mode": "live", "plan_usable": True, "plan_id": "b" * 16,
                            "plan_age_min": 1, "plan_expires_in_min": 80, "control_blocked": "plan_id_already_applied"})
        assert "Plan not applied: plan_id_already_applied" in text
