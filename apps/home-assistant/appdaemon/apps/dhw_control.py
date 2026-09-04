import asyncio
import base64
import functools
import json
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List

import appdaemon.plugins.hass.hassapi as hass

from utils import escape_markdownv2, negative_price_notification, retry_with_backoff


@dataclass
class Price:
    value: float
    datetime: datetime


def chunk_list(lst: List, chunk_size: int) -> List[List]:
    """Split a list into chunks of specified size."""
    return [lst[i : i + chunk_size] for i in range(0, len(lst), chunk_size)]


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
        # Register schedulers if dhw control is enabled
        await self.listen_state(self._register_schedulers, input_boolean_enable, new="on", old="off")
        # Unregister schedulers if dhw control is enabled
        await self.listen_state(self._unregister_schedulers, input_boolean_enable, new="off", old="on")

        # Register schedulers every day
        # give enough time to get new data
        await self.run_daily(self._daily_register_schedulers, "00:00:30")

        await self.create_task(self._daily_register_schedulers())

    async def _daily_register_schedulers(self, _entity="", _attribute="", _old="", _new="", _kwargs={}):
        is_enabled = await self.get_state(self.args["input_boolean"]["enable"], attribute="state") == "on"
        self.log(f"DHW control is {'enabled' if is_enabled else 'disabled'}")
        if is_enabled:
            try:
                await self._register_schedulers()
            except Exception as e:
                self.log(f"Error during daily scheduler registration: {e}", level="ERROR")
                try:
                    await self.notify(
                        escape_markdownv2(
                            f"""Error during daily scheduler registration: {e}

Retrying in 10 minutes"""
                        ),
                        name=self.args["notify"]["target"],
                    )
                except Exception as notify_error:
                    self.log(f"Error sending notification: {notify_error}", level="ERROR")
                self.log("Retrying in 10 minutes")
                await asyncio.sleep(600)
                await self._daily_register_schedulers()

    async def _get_prices(self) -> List[Price]:
        pvpc = await retry_with_backoff(
            lambda: self.get_state(self.args["sensor"]["pvpc_price"], attribute="all"),
            max_retries=3,
            initial_delay=2.0,
            operation_name="get_pvpc_price",
        )
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
        msg = f"Force DHW{dry_run_msg}"
        self.log(msg)
        if self.args["notify"]["enabled"]:
            await self.notify(escape_markdownv2(msg), name=self.args["notify"]["target"])
        if self.args["dhw"]["enabled"]:
            force_dhw_entity = self.get_entity(self.args["dhw"]["entity"])
            await force_dhw_entity.turn_on()

    def _generate_vega_diagram(self, datetimes_to_schedule: List[datetime]) -> str:
        scheduled_hours = {dt.hour for dt in datetimes_to_schedule}
        data_values = [{"hour": i, "status": "ON" if i in scheduled_hours else "OFF"} for i in range(24)]

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
            self.log(f"Error getting prices: {e}", level="ERROR")
            await self.notify(
                escape_markdownv2("Error getting prices: using fallback schedule, retrying in 10 minutes"),
                name=self.args["notify"]["target"],
            )
            await self._register_fallback_schedule()
            await asyncio.sleep(600)
            return await self._register_schedulers()

        self.log(f"{prices=}", level="DEBUG")

        negative_msg = negative_price_notification(prices)
        if negative_msg:
            self.log(negative_msg, level="WARNING")

        # Get interval_hours from config (default 24 = once per day)
        interval_hours = self.args.get("interval_hours", 24)
        if interval_hours not in (1, 2, 3, 4, 6, 8, 12, 24):
            self.log(f"Invalid interval_hours {interval_hours}, must be a divisor of 24. Using 24.", level="WARNING")
            interval_hours = 24

        # Split prices into chunks based on interval and find cheapest in each
        price_chunks = chunk_list(prices, interval_hours)
        cheapest_prices = [min(chunk, key=lambda x: x.value) for chunk in price_chunks if chunk]
        self.log(f"{interval_hours=}, {cheapest_prices=}", level="DEBUG")

        datetimes_to_schedule = [p.datetime for p in cheapest_prices]

        if self.args["notify"]["enabled"]:
            vega_diagram = self._generate_vega_diagram(datetimes_to_schedule)
            hours_str = ", ".join(dt.strftime("%H:%M") for dt in datetimes_to_schedule)
            negative_line = f"\n{negative_msg}" if negative_msg else ""
            escaped_text = escape_markdownv2(f"Programming the DHW control for these hours: {hours_str} ")
            link = f"[​​​​​​​​​​​](https://kroki.grigri.cloud/vegalite/png/{vega_diagram})"
            msg = f"{escaped_text}{link}{escape_markdownv2(negative_line)}"
            await self.notify(msg, name=self.args["notify"]["target"])

        await self._schedule_dhw(datetimes_to_schedule)

    async def _register_fallback_schedule(self):
        interval_hours = self.args.get("interval_hours", 24)
        if interval_hours not in (1, 2, 3, 4, 6, 8, 12, 24):
            interval_hours = 24
        now = datetime.now(self.get_timezone())
        current_hour = datetime(now.year, now.month, now.day, now.hour)
        datetimes_to_schedule = [datetime(now.year, now.month, now.day, h) for h in range(0, 24, interval_hours)]
        # if all window starts are already past, force DHW now instead of skipping the day
        if all(slot <= current_hour for slot in datetimes_to_schedule):
            datetimes_to_schedule.insert(0, current_hour)
        self.log(f"Registering fallback schedule: {datetimes_to_schedule}", level="WARNING")
        await self._schedule_dhw(datetimes_to_schedule)

    async def _schedule_dhw(self, datetimes_to_schedule):
        now = datetime.now(self.get_timezone())
        current_hour = datetime(now.year, now.month, now.day, now.hour)

        for datetime_to_schedule in datetimes_to_schedule:
            if current_hour.hour == datetime_to_schedule.hour:
                await self._force_dhw()
            elif current_hour < datetime_to_schedule:
                self.log(f"Registering {self._force_dhw.__name__} at {datetime_to_schedule.strftime('%H:%M:%S')}", level="INFO")
                self.timers.append(await self.run_at(self._force_dhw, datetime_to_schedule.strftime("%H:%M:%S")))

    async def _unregister_schedulers(self, _entity="", _attribute="", _old="", _new="", _kwargs={}):
        self.log("Unregistering schedulers")

        schedulers = {_id: self.AD.sched.schedule[self.name][_id] for _id in self.AD.sched.schedule.get(self.name, {})}
        self.log(f"{schedulers=}", level="DEBUG")
        # callback is wrapped in a functools.partial, so we need to access the func attribute
        def compare_callback(callback, func):
                return callback.func == func if isinstance(callback, functools.partial) else callback == func
        ids_to_disable = [_id for _id, i in schedulers.items() if not compare_callback(i["callback"], self._daily_register_schedulers)]
        self.log(f"{ids_to_disable=}", level="DEBUG")
        [await self.cancel_timer(_id) for _id in ids_to_disable]

    async def terminate(self):
        await self._unregister_schedulers()
