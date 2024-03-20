import datetime

import appdaemon.plugins.hass.hassapi as hass


class LastConnectionSensor(hass.Hass):
    async def initialize(self):
        time = datetime.time(0, 0, 0)
        self.run_minutely(self.update_last_connection, time)

    async def update_last_connection(self, cb_args):
        now = datetime.datetime.now()
        self.set_state("sensor.last_appdaemon_connection", state=now.strftime("%Y-%m-%d %H:%M:%S"))
