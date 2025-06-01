import asyncio
import base64
import json
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List

import appdaemon.plugins.hass.hassapi as hass


@dataclass
class Price:
    value: float
    datetime: datetime

    def __post_init__(self):
        if self.value < 0:
            raise ValueError


def is_float(value):
    try:
        _ = float(value)
        return True
    except ValueError:
        return False


class DHWControl(hass.Hass):
    async def initialize(self):
        self.log("Starting")
        self.stop_app
        self.timers = []
        self._logging
        input_boolean_enable = self.args["input_boolean"]["enable"]
        await self._daily_register_schedulers()
        # Register schedulers if dhw control is enabled
        await self.listen_state(self._register_schedulers, input_boolean_enable, new="on", old="off")
        # Unregister schedulers if dhw control is enabled
        await self.listen_state(self._unregister_schedulers, input_boolean_enable, new="off", old="on")

        # Register schedulers every day
        # give enough time to get new data
        await self.run_daily(self._daily_register_schedulers, "00:00:30")

    async def _daily_register_schedulers(self, _entity="", _attribute="", _old="", _new="", _kwargs={}):
        is_enabled = await self.get_state(self.args["input_boolean"]["enable"], attribute="state") == "on"
        self.log(f"DHW control is {'enabled' if is_enabled else 'disabled'}")
        if is_enabled:
            await self._register_schedulers()

    async def _get_prices(self) -> List[Price]:
        pvpc = await self.get_state(self.args["sensor"]["pvpc_price"], attribute="all")
        self.log(f"{pvpc=}", level="DEBUG")
        now = datetime.now(self.get_timezone())

        prices = [
            Price(
                datetime=datetime(now.year, now.month, now.day) + timedelta(hours=i),
                value=pvpc["attributes"][f"price_{i:02d}h"],
            )
            for i in range(24)
            if f"price_{i:02d}h" in pvpc["attributes"] and is_float(pvpc["attributes"][f"price_{i:02d}h"])
        ]
        return prices

    async def _force_dhw(self, kwargs={}):
        dry_run_msg = "" if self.args["dhw"]["enabled"] else " (dry run mode)"
        msg = f"Set force DHW mode{dry_run_msg}"
        self.log(msg)
        if self.args["notify"]["enabled"]:
            await self.notify(msg, name=self.args["notify"]["target"])
        if self.args["dhw"]["enabled"]:
            force_dhw_entity = self.get_entity(self.args["dhw"]["entity"])
            await force_dhw_entity.turn_on()

    def _generate_vega_diagram(self, datetime_to_schedule: datetime) -> str:
        data_values = [{"hour": i, "status": "ON" if i == datetime_to_schedule.hour else "OFF"} for i in range(24)]

        vega_lite_spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "data": {"values": data_values},
            "mark": "rect",
            "encoding": {
                "x": {"field": "hour", "type": "ordinal", "title": "Hour of Day"},
                "color": {
                    "field": "status",
                    "type": "nominal",
                    "scale": {"domain": ["ON", "OFF"], "range": ["green", "red"]},
                },
                "tooltip": [{"field": "hour", "title": "Hour"}, {"field": "status", "title": "Status"}],
            },
            "config": {"axisX": {"labelAngle": 0, "labelAlign": "right"}},
            "width": 400,
            "height": 20,
        }

        vega_lite_json = json.dumps(vega_lite_spec, indent=2)
        self.log(vega_lite_json, level="DEBUG")

        return base64.urlsafe_b64encode(zlib.compress(vega_lite_json.encode("utf-8"), 9)).decode("ascii")

    async def _register_schedulers(self, _entity="", _attribute="", _old="", _new="", _kwargs={}):
        await self._unregister_schedulers()
        self.log("Registering schedulers")

        try:
            prices = await self._get_prices()
        except Exception as e:
            # Retry in 10 minutes
            self.log(f"Error getting prices: {e}", level="ERROR")
            await self.notify(
                "Error getting prices: retrying in 10 minutes",
                name=self.args["notify"]["target"],
            )
            await asyncio.sleep(600)
            return self._register_schedulers()

        self.log(f"{prices=}", level="DEBUG")
        cheapest_price = min(prices, key=lambda x: x.value)
        self.log(f"{cheapest_price=}", level="DEBUG")

        datetime_to_schedule = cheapest_price.datetime

        if self.args["notify"]["enabled"]:
            vega_diagram = self._generate_vega_diagram(datetime_to_schedule)
            msg = f"Programming the DHW control for this hour: [​​​​​​​​​​​](https://kroki.grigri.cloud/vegalite/png/{vega_diagram})"
            await self.notify(msg, name=self.args["notify"]["target"])

        now = datetime.now(self.get_timezone())
        current_hour = datetime(now.year, now.month, now.day, now.hour)
        if current_hour.hour == datetime_to_schedule.hour:
            await self._force_dhw()
        elif current_hour < datetime_to_schedule:
            self.timers.append(await self.run_at(self._force_dhw, datetime_to_schedule.strftime("%H:%M:%S")))

    async def _unregister_schedulers(self, _entity="", _attribute="", _old="", _new="", _kwargs={}):
        self.log("Unregistering schedulers")

        schedulers = {_id: self.AD.sched.schedule[self.name][_id] for _id in self.AD.sched.schedule.get(self.name, {})}
        self.log(f"{schedulers=}", level="DEBUG")
        ids_to_disable = [_id for _id, i in schedulers.items() if i["callback"] != self._daily_register_schedulers]
        self.log(f"{ids_to_disable=}", level="DEBUG")
        [await self.cancel_timer(_id) for _id in ids_to_disable]

    async def terminate(self):
        await self._unregister_schedulers()
