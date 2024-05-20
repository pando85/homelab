#!/usr/bin/env -S python -u
# encoding: utf-8

from aiohttp import web
import asyncio
import RPi.GPIO
import os
import logging

# Settings
FAN_GPIO = 12  # Fan GPIO pin number (default: 12)
IDLE_SPEED = 50  # Fan speed when under min_temp (IDLE) (min: 0, max: 100)
MIN_TEMP = 50  # Fan starting temperature
MAX_TEMP = 60  # Fan max speed temperature (max: 60)
SOCKET_PATH = "/run/kvmd/fan.sock"  # UNIX socket path
SLEEP_TIME = 5

# Define GPIO
RPi.GPIO.setwarnings(False)
RPi.GPIO.setmode(RPi.GPIO.BCM)
RPi.GPIO.setup(FAN_GPIO, RPi.GPIO.OUT)
pwm = RPi.GPIO.PWM(FAN_GPIO, 10)
running = True

# Setup logging
logging.basicConfig(level=logging.INFO)

set_speed = None
cpu_temp = None


def calculate_fan_speed(cpu_temp):
    if cpu_temp >= MAX_TEMP:
        return 100
    else:
        temp_speed = int((cpu_temp - MIN_TEMP) / (MAX_TEMP - MIN_TEMP) * 100)
        return min(max(IDLE_SPEED, temp_speed), 100)


routes = web.RouteTableDef()


@routes.get("/")
async def root(_):
    return web.json_response({"ok": True, "result": {"version": "1.0"}})


@routes.get("/state")
async def handle_request(_):
    return web.json_response({"ok": True, "result": {"temp": cpu_temp, "fan_speed": set_speed}})


async def update_temp_and_fan_speed():
    global cpu_temp, set_speed, running
    try:
        logging.info("Init PWM and set speed to 0")
        pwm.start(0)
        pwm.ChangeDutyCycle(0)
        while True:
            with open("/sys/class/thermal/thermal_zone0/temp") as tmpFile:
                cpu_temp = int(tmpFile.read()) / 1000
            logging.info("Temp: %s", cpu_temp)
            if cpu_temp >= MIN_TEMP:
                if not running:
                    logging.info("PWM started")
                    pwm.start(0)
                    running = True
                set_speed = calculate_fan_speed(cpu_temp)
                logging.info("set_speed: %s %%", set_speed)
                pwm.ChangeDutyCycle(set_speed)
            else:
                if running:
                    running = False
                    pwm.stop()
                    logging.info("PWM stopped")
            await asyncio.sleep(SLEEP_TIME)
    except Exception as e:
        logging.error("Error: %s", e)


async def main():
    app = web.Application()
    app.add_routes(routes)

    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)

    runner = web.AppRunner(app)
    await runner.setup()
    asyncio.create_task(update_temp_and_fan_speed())

    try:
        site = web.UnixSite(runner, SOCKET_PATH)
        await site.start()
        logging.info(f"Fan control server started at {SOCKET_PATH}")
        os.chmod(SOCKET_PATH, 0o666)
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        await runner.cleanup()
        pwm.stop()


if __name__ == "__main__":
    asyncio.run(main())
