#!/usr/bin/env python3
"""History-preserving handover of the two shellyem-main power sensors to MQTT.

Why remove-then-promote, and not rename:

A registry *rename* makes HA's recorder migrate history in place
(`UpdateStatesMetadataTask` / `UpdateStatisticsMetadataTask`), so renaming the
native entity away from `sensor.shellyem_main_channel_N_power` would move the
whole production history to the new ID. Renaming the MQTT probe onto the freed
canonical ID then leaves that ID starting from zero rows.

A registry *removal* does not match the recorder's rename filter, so the
canonical `states_meta` row survives. Promoting the MQTT probe afterwards hits
`StatesMetaManager.update_metadata`'s collision guard, which refuses to migrate
when the target ID already exists — exactly what we want: production history
stays under the canonical ID and the MQTT values append to the same metadata_id.

`rollback` must run only after the MQTT YAML has been reverted and HA has
restarted, so no MQTT entity still owns a canonical ID. Renaming an MQTT entity
away from canonical would otherwise drag the production history with it.

Reads the token from HA_TOKEN_FILE (default /tmp/opencode/ha_token). Never
touches the database or the registry files directly.
"""

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import websockets

URL = "wss://hass.grigri.cloud/api/websocket"
SHELLY_ENTRY_TITLE = "shellyem-main"
CHANNELS = (1, 2)
CANONICAL = {c: f"sensor.shellyem_main_channel_{c}_power" for c in CHANNELS}
NATIVE_UNIQUE = {c: f"244CAB417668-emeter_{c - 1}-power" for c in CHANNELS}
MQTT_UNIQUE = {c: f"shellyem_main_channel_{c}_power_mqtt" for c in CHANNELS}
MAX_STALE_S = 30  # 15x the 2 s publish period


def fail(message):
    raise SystemExit(f"ABORT: {message}")


async def run(action):
    token = Path(os.environ.get("HA_TOKEN_FILE", "/tmp/opencode/ha_token")).read_text().strip()
    async with websockets.connect(URL, max_size=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            fail("HA authentication failed")
        request_id = 0

        async def command(kind, **kwargs):
            nonlocal request_id
            request_id += 1
            await ws.send(json.dumps({"id": request_id, "type": kind, **kwargs}))
            while True:
                response = json.loads(await ws.recv())
                if response.get("id") == request_id and response.get("type") == "result":
                    if not response.get("success"):
                        fail(f"{kind} failed: {response.get('error')}")
                    return response.get("result")

        async def entries():
            return await command("config/entity_registry/list")

        def find(registry, platform, unique_id):
            return next((e for e in registry
                         if e["platform"] == platform and e["unique_id"] == unique_id), None)

        def owners(registry, entity_id):
            return [e for e in registry if e["entity_id"] == entity_id]

        async def update(entry, **changes):
            await command("config/entity_registry/update", entity_id=entry["entity_id"], **changes)
            print(f"CHANGED {entry['entity_id']} -> {changes}")

        registry = await entries()

        if action == "inspect":
            for channel in CHANNELS:
                native = find(registry, "shelly", NATIVE_UNIQUE[channel])
                mqtt = find(registry, "mqtt", MQTT_UNIQUE[channel])
                for label, entry in (("native", native), ("mqtt", mqtt)):
                    if entry is None:
                        print(f"ch{channel} {label}: absent")
                    else:
                        print(f"ch{channel} {label}: {entry['entity_id']} "
                              f"disabled={entry['disabled_by']} name={entry['name']!r}")
                canonical_owners = owners(registry, CANONICAL[channel])
                described = [f"{o['platform']}:{o['unique_id']}" for o in canonical_owners]
                print(f"ch{channel} canonical owner: {described}")
            return

        if action == "promote":
            states = {s["entity_id"]: s for s in await command("get_states")}
            plan = []
            for channel in CHANNELS:
                canonical = CANONICAL[channel]
                native = find(registry, "shelly", NATIVE_UNIQUE[channel])
                mqtt = find(registry, "mqtt", MQTT_UNIQUE[channel])
                if native is None or mqtt is None:
                    fail(f"ch{channel}: native={native is not None} mqtt={mqtt is not None} "
                         "— deploy and verify the MQTT probe first")
                if native["entity_id"] != canonical:
                    fail(f"ch{channel}: native entity is {native['entity_id']}, expected {canonical}")
                if len(owners(registry, canonical)) != 1:
                    fail(f"ch{channel}: {canonical} is claimed by "
                         f"{len(owners(registry, canonical))} registry entries")
                if mqtt["disabled_by"] is not None:
                    fail(f"ch{channel}: MQTT probe is disabled")
                state = states.get(mqtt["entity_id"])
                if state is None:
                    fail(f"ch{channel}: MQTT probe has no state yet")
                try:
                    float(state["state"])
                except (TypeError, ValueError):
                    fail(f"ch{channel}: MQTT probe state is {state['state']!r}, not numeric")
                if state["attributes"].get("unit_of_measurement") != "W":
                    fail("ch{channel}: MQTT probe must report W")
                if state["attributes"].get("friendly_name") != f"shellyem-main Channel {channel} Power":
                    fail(f"ch{channel}: friendly_name {state['attributes'].get('friendly_name')!r} "
                         "breaks the Prometheus label contract")
                plan.append((channel, canonical, native, mqtt, state))

            # Refuse to promote onto a stream that has already gone quiet.
            newest = max(state["last_updated"] for *_, state in plan)
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(newest)).total_seconds()
            if age > MAX_STALE_S:
                fail(f"MQTT stream is {age:.0f} s stale")

            print(f"MQTT stream age {age:.1f} s; promoting both channels")
            for _channel, _canonical, native, _mqtt, _state in plan:
                # Removal, not rename: keeps the canonical recorder history in place.
                await command("config/entity_registry/remove", entity_id=native["entity_id"])
                print(f"CHANGED removed native {native['entity_id']} (unique_id {native['unique_id']})")
            for _channel, canonical, _native, mqtt, _state in plan:
                await update(mqtt, new_entity_id=canonical)

            await reload_shelly(command)

            registry = await entries()
            for channel in CHANNELS:
                recreated = find(registry, "shelly", NATIVE_UNIQUE[channel])
                if recreated is None:
                    fail(f"ch{channel}: native entity was not recreated after reload")
                if recreated["entity_id"] == CANONICAL[channel]:
                    fail(f"ch{channel}: native reclaimed {CANONICAL[channel]} — MQTT promotion lost")
                if recreated["disabled_by"] is None:
                    await update(recreated, disabled_by="user")

        elif action == "rollback":
            for channel in CHANNELS:
                if any(e["platform"] == "mqtt" and e["entity_id"] == CANONICAL[channel]
                       for e in registry):
                    fail(f"ch{channel}: an MQTT entity still owns {CANONICAL[channel]}. "
                         "Revert the MQTT YAML, restart HA, then re-run rollback — renaming it "
                         "away would migrate the production history with it")
            for channel in CHANNELS:
                native = find(registry, "shelly", NATIVE_UNIQUE[channel])
                if native is None:
                    fail(f"ch{channel}: no native power entity to restore")
                if native["entity_id"] == CANONICAL[channel] and native["disabled_by"] is None:
                    print(f"ch{channel}: already canonical and enabled")
                    continue
                await update(native, new_entity_id=CANONICAL[channel], disabled_by=None)
            await reload_shelly(command)

        for entry in await entries():
            if entry["unique_id"] in set(NATIVE_UNIQUE.values()) | set(MQTT_UNIQUE.values()):
                print(f"FINAL {entry['entity_id']} platform={entry['platform']} "
                      f"unique_id={entry['unique_id']} disabled={entry['disabled_by']}")


async def reload_shelly(command):
    entries = await command("config_entries/get", domain="shelly")
    entry = next((e for e in entries if e["title"] == SHELLY_ENTRY_TITLE), None)
    if entry is None:
        fail(f"no shelly config entry titled {SHELLY_ENTRY_TITLE}")
    await command("call_service", domain="homeassistant", service="reload_config_entry",
                  service_data={"entry_id": entry["entry_id"]})
    print(f"CHANGED reloaded shelly entry {entry['entry_id']}")
    await asyncio.sleep(8)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("inspect", "promote", "rollback"))
    asyncio.run(run(parser.parse_args().action))
