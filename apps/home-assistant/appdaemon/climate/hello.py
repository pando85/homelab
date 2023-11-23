import appdaemon.plugins.hass.hassapi as hass

#
# Hello World App
#
# Args:
#

class HelloWorld(hass.Hass):

  async def initialize(self):
    self.log("Hello from AppDaemon")
    self.log("You are now ready to run Apps!")
