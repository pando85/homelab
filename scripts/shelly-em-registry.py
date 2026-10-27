#!/usr/bin/env python3
"""Idempotent, reversible power-only MQTT handover via HA's supported registry API.

Run through metal/playbooks/shelly-em.yaml --tags registry. Authentication is read
from HA_TOKEN_FILE (default /tmp/opencode/ha_token), never stored in this repo.
Deploy/validate the MQTT sensors under their temporary IDs before promotion.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

import websockets

URL = "wss://hass.grigri.cloud/api/websocket"


async def run(action):
    token = Path(os.environ.get("HA_TOKEN_FILE", "/tmp/opencode/ha_token")).read_text().strip()
    async with websockets.connect(URL, max_size=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            raise RuntimeError("HA authentication failed")
        request_id = 0

        async def command(kind, **kwargs):
            nonlocal request_id
            request_id += 1
            await ws.send(json.dumps({"id": request_id, "type": kind, **kwargs}))
            while True:
                response = json.loads(await ws.recv())
                if response.get("id") == request_id and response.get("type") == "result":
                    if not response.get("success"):
                        raise RuntimeError(f"{kind} failed: {response.get('error')}")
                    return response.get("result")

        async def entries():
            return await command("config/entity_registry/list")

        async def update(entry, **changes):
            if all(entry.get(key) == value for key, value in changes.items() if key != "new_entity_id"):
                if "new_entity_id" not in changes or entry["entity_id"] == changes["new_entity_id"]:
                    return
            await command("config/entity_registry/update", entity_id=entry["entity_id"], **changes)
            print(f"CHANGED {entry['entity_id']}: {changes}")

        registry = await entries()
        pairs = []
        for channel in (1, 2):
            canonical = f"sensor.shellyem_main_channel_{channel}_power"
            native = next(e for e in registry if e["platform"] == "shelly"
                          and e["unique_id"] == f"244CAB417668-emeter_{channel - 1}-power")
            mqtt = next((e for e in registry if e["platform"] == "mqtt"
                         and e["unique_id"] == f"shellyem_main_channel_{channel}_power_mqtt"), None)
            if action != "inspect" and mqtt is None:
                raise RuntimeError(f"Missing validated MQTT entity for channel {channel}")
            if any(e["entity_id"] == canonical and e["id"] not in
                   {native["id"], mqtt["id"] if mqtt else None} for e in registry):
                raise RuntimeError(f"Unrelated registry entry owns {canonical}")
            pairs.append((channel, canonical, native, mqtt))

        # Preflight both MQTT sensors before changing either native entity.
        if action == "promote":
            states = {s["entity_id"]: s for s in await command("get_states")}
            for channel, _, _, mqtt in pairs:
                state = states.get(mqtt["entity_id"], {})
                try:
                    float(state.get("state"))
                except (TypeError, ValueError) as error:
                    raise RuntimeError(f"MQTT channel {channel} is not reporting numeric power") from error
                if state.get("attributes", {}).get("unit_of_measurement") != "W":
                    raise RuntimeError("MQTT power must use W")

        for channel, canonical, native, mqtt in pairs:
            if action == "promote":
                await update(native, disabled_by="user")
                # Disabling is immediate, but a disabled registry entry still reserves its ID.
                native = next(e for e in await entries() if e["id"] == native["id"])
                await update(native, new_entity_id=f"{canonical}_native")
                mqtt = next(e for e in await entries() if e["id"] == mqtt["id"])
                await update(mqtt, new_entity_id=canonical,
                             name=f"shellyem-main Channel {channel} Power", disabled_by=None)
            elif action == "rollback":
                await update(mqtt, new_entity_id=f"{canonical}_mqtt_probe")
                native = next(e for e in await entries() if e["id"] == native["id"])
                await update(native, new_entity_id=canonical, disabled_by=None)

        for entry in await entries():
            if entry["unique_id"] in {f"244CAB417668-emeter_{i}-power" for i in (0, 1)} or (
                entry["unique_id"] in {f"shellyem_main_channel_{i}_power_mqtt" for i in (1, 2)}
            ):
                print(f"{entry['entity_id']} platform={entry['platform']} disabled={entry['disabled_by']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "promote", "rollback"))
    asyncio.run(run(parser.parse_args().action))
