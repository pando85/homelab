from dataclasses import dataclass
from typing import List

import appdaemon.plugins.hass.hassapi as hass

"""
Requirements:
- button for enable or disable automated control.
- calculate number of minimum hours needed: can be fixed at the beginning ~ 5, then we can use
    outside temperatures and then we can combine it with predictions and sun hours in the morning.
- calculate cheapest hours (use price moving average for last week -> lower than that is OK)
    if cheapest hours are much cheaper for more hours add more hours.
"""


@dataclass
class Price:
    value: float
    hour: int

    def __post_init__(self):
        if self.value < 0:
            raise ValueError


class ClimateControl(hass.Hass):
    async def initialize(self):
        self.log("Starting")
        self.stop_app
        self.timers = []

        prices = await self._get_prices()
        self.log(f"{prices}")

        input_boolean_enable = self.args["input_boolean"]["enable"]
        is_enabled = await self.get_state(input_boolean_enable, attribute="state") == "on"
        self.log(f"Climate control is {'enabled' if is_enabled else 'disabled'}")
        if is_enabled:
            await self._register_schedulers()

        self.listen_state(self._register_schedulers, input_boolean_enable, new="on", old="off")
        self.listen_state(self._unregister_schedulers, input_boolean_enable, new="off", old="on")

    async def _get_prices(self) -> List[Price]:
        pvpc = await self.get_state(self.args["sensor"]["pvpc_price"], attribute="all")
        prices = [Price(hour=i, value=pvpc["attributes"][f"price_{i:02d}h"]) for i in range(24)]
        return prices

    async def _start_heating(self, kwargs={}):
        self.log("Start heating")

    async def _stop_heating(self, kwargs={}):
        self.log("Stop heating")

    async def _register_schedulers(self, _entity="", _attribute="", _old="", _new="", _kwargs={}):
        self.log("Registering schedulers")

        async def register(*args):
            self.log(f"Registering {args}", level="INFO")
            self.timers.append(await self.run_at(*args))

        await register(self._start_heating, "20:12:50")
        await register(self._start_heating, "20:13:00")
        self.timers.append(await self.run_in(self._start_heating, 5))
        self.timers.append(await self.run_in(self._start_heating, 2))

    async def _unregister_schedulers(self, _entity="", _attribute="", _old="", _new="", _kwargs={}):
        self.log("Unregistering schedulers")
        await self.notify("yea", name=self.args["notify_target"])
        # get a list to iterate because keys change dynamically while iterating
        timers = list(self.AD.sched.schedule[self.name].keys())
        [await self.cancel_timer(timer) for timer in timers]

    async def terminate(self):
        self._unregister_schedulers()
