"""ClimatePlan v1 documents for the tests (synthetic): the shape of the optimizer repository's
docs/climate-plan-contract.md and docs/schemas/climate-plan-v1.json, built here, never fetched."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

CLIMATE_MODE = "input_select.climate_optimizer_mode"
UTC = timezone.utc
STEP = timedelta(minutes=15)


def instant(value):
    return value.astimezone(UTC).isoformat()


def hour_of(now):
    return now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def make_plan(t0, heating=1, water=33, n=96, ttl=timedelta(minutes=90), run_delay=timedelta(minutes=10), **overrides):
    """A planned plan for the decision boundary `t0` (UTC). `heating` / `water` are a constant or a
    function of the slot start (a UTC datetime). Top-level keys in `overrides` replace the plan's."""
    t0 = t0.astimezone(UTC)
    starts = [t0 + i * STEP for i in range(n)]
    heat = [int(heating(s)) if callable(heating) else int(heating) for s in starts]
    level = [water(s) if callable(water) else water for s in starts]
    level = [w if h else None for w, h in zip(level, heat)]
    plan_id = hashlib.sha256(json.dumps([instant(t0), heat, level]).encode()).hexdigest()[:16]
    plan = {
        "version": 1,
        "advisory": True,
        "generated_at": instant(t0 + run_delay),
        "valid_until": instant(t0 + ttl),
        "t0": instant(t0),
        "code_version": "0.12.0",
        "regime": "comfort",
        "status": "planned",
        "reason": None,
        "model": {"prior_id": None, "model_hash": None, "mpc_config_sha256": None, "solver_status": "optimal"},
        "inputs": {
            "comfort": {"bedroom": {"floor_c": 21.0, "day_min_c": 21.0, "floor_from_helper": True, "day_min_from_helper": True}},
            "away": False,
            "weather": "forecast",
        },
        "slots": {
            "start": instant(t0),
            "step_minutes": 15,
            "n": n,
            "heating": heat,
            "water_c": level,
            "dhw": [0] * n,
            "predicted_room_c": {"bedroom": [21.5] * n},
        },
        "plan_id": plan_id,
        "requested_mode": "live",
        "effective_mode": "shadow",
        "live_locked": True,
        "live_lock_reason": "ADR-0008 proposed, not accepted; homelab hard lock optimizer_live_allowed=false",
        "mode_source": "helper",
        "summary": {"as_of": instant(t0 + run_delay), "age_s": 0, "expires_in_s": 4800},
    }
    plan.update(overrides)
    return plan


def answer(plan, status=200):
    return status, json.dumps(plan)


def hourly_server(heating=1, water=33, **kwargs):
    """What the optimizer serves over a simulated day: a new plan at about every hour + 10 minutes
    (the previous one, valid for 90 minutes, until then). A function of `now` -> (status, body)."""

    def serve(now):
        boundary = hour_of(now)
        if now.astimezone(UTC) - boundary < timedelta(minutes=10):
            boundary -= timedelta(hours=1)
        return answer(make_plan(boundary, heating=heating, water=water, **kwargs))

    return serve
