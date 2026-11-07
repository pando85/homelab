#!/usr/bin/env python3
"""Read the entities exposed to Assist, their areas, floors and aliases from Home Assistant,
READ-ONLY, and write them in the shape of agent_eval/inventory_es.yaml so ha_fake.FakeHome can
load them.

Only list/get websocket commands are sent (no service call, no conversation, no state change):
  config/area_registry/list, config/floor_registry/list, config/entity_registry/list,
  config/device_registry/list, homeassistant/expose_entity/list (admin token), get_states.

The output holds the home's real names: write it ONLY to a private, git-ignored directory.

  python fetch_inventory.py --token-file /path/0600-file --out $KIVI_PRIVATE/decision-models/inventory_real.yaml
"""
import argparse
import asyncio
import json
import os
import re
import ssl
import sys

# Domains Assist can act on or read and that the decision stage cares about.
DOMAINS = {"light", "switch", "fan", "cover", "climate", "media_player", "valve", "lock", "vacuum",
           "sensor", "binary_sensor", "weather", "scene", "script", "input_boolean", "humidifier",
           "water_heater", "lawn_mower", "button", "todo", "calendar"}


def slug(text):
    s = text.lower()
    for a, b in zip("áéíóúüñ", "aeiouun"):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "x"


def build_inventory(areas, floors, entities, devices, exposed, states, satellite_area=None):
    """Pure: registry lists and states -> inventory dict (FakeHome shape). Only entities exposed to
    the conversation assistant are kept, as Home Assistant's default agent does."""
    st = {s["entity_id"]: s for s in states}
    dev_area = {d["id"]: d.get("area_id") for d in devices}
    reg = {e["entity_id"]: e for e in entities}
    floor_out = {f["floor_id"]: {"name": f["name"]} for f in floors}
    area_out = {}
    for a in areas:
        item = {"name": a["name"], "aliases": sorted(a.get("aliases") or [])}
        if a.get("floor_id"):
            item["floor"] = a["floor_id"]
        area_out[a["area_id"]] = item
    ent_out = {}
    for eid, flags in exposed.items():
        if not (flags or {}).get("conversation"):
            continue
        if eid.split(".")[0] not in DOMAINS or eid not in st:
            continue
        s, r = st[eid], reg.get(eid, {})
        name = s.get("attributes", {}).get("friendly_name") or r.get("name") or r.get("original_name") or eid
        area = r.get("area_id") or dev_area.get(r.get("device_id"))
        item = {"name": name, "state": s.get("state")}
        if r.get("aliases"):
            item["aliases"] = sorted(a for a in r["aliases"] if a)
        if area:
            item["area"] = area
        dc = s.get("attributes", {}).get("device_class") or r.get("device_class") or r.get("original_device_class")
        if dc:
            item["device_class"] = dc
        attrs = {k: v for k, v in s.get("attributes", {}).items()
                 if k in ("brightness", "current_position", "temperature", "current_temperature", "percentage",
                          "volume_level", "unit_of_measurement", "device_class")}
        if attrs:
            item["attributes"] = attrs
        ent_out[eid] = item
    used_floors = {a.get("floor") for a in area_out.values()}
    floor_out = {k: v for k, v in floor_out.items() if k in used_floors} or {"none": {"name": "Casa"}}
    for a in area_out.values():
        a.setdefault("floor", next(iter(floor_out)))
    return {"now": "2026-11-05T18:42:00+01:00", "timezone": "Europe/Madrid",
            "satellite_area": satellite_area or next(iter(area_out)),
            "floors": floor_out, "areas": area_out, "entities": ent_out,
            "calendar_events": {"today": [], "week": []}, "timer_status_seconds_left": 300}


async def fetch(url, token):
    import websockets
    ctx = ssl.create_default_context()  # TLS verified
    async with websockets.connect(url, ssl=ctx, max_size=64 * 2 ** 20) as ws:
        hello = json.loads(await ws.recv())
        assert hello["type"] == "auth_required", hello["type"]
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(await ws.recv())
        if auth["type"] != "auth_ok":
            raise SystemExit("authentication failed")
        out, mid = {}, 0
        for cmd in ("config/area_registry/list", "config/floor_registry/list", "config/entity_registry/list",
                    "config/device_registry/list", "homeassistant/expose_entity/list", "get_states"):
            mid += 1
            await ws.send(json.dumps({"id": mid, "type": cmd}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == mid:
                    break
            if not msg.get("success"):
                raise SystemExit(f"{cmd} failed: {msg.get('error', {}).get('code')}")
            out[cmd] = msg["result"]
        return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="wss://hass.grigri.cloud/api/websocket")
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--satellite-area", default=None, help="area id of the satellite (default: first area)")
    a = ap.parse_args()
    with open(a.token_file) as fh:
        token = fh.read().strip()
    r = asyncio.run(fetch(a.url, token))
    exposed = r["homeassistant/expose_entity/list"].get("exposed_entities", {})
    inv = build_inventory(r["config/area_registry/list"], r["config/floor_registry/list"],
                          r["config/entity_registry/list"], r["config/device_registry/list"], exposed,
                          r["get_states"], a.satellite_area)
    import yaml
    old = os.umask(0o077)
    with open(a.out, "w", encoding="utf-8") as fh:
        yaml.safe_dump(inv, fh, allow_unicode=True, sort_keys=False)
    os.umask(old)
    by_dom = {}
    for eid in inv["entities"]:
        by_dom[eid.split(".")[0]] = by_dom.get(eid.split(".")[0], 0) + 1
    # Counts only on stdout, no names.
    print(f"areas {len(inv['areas'])}, floors {len(inv['floors'])}, exposed entities {len(inv['entities'])}: "
          + ", ".join(f"{k} {v}" for k, v in sorted(by_dom.items())), file=sys.stderr)


if __name__ == "__main__":
    main()
