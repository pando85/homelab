#!/usr/bin/env -S python -u
# encoding: utf-8

import RPi.GPIO
import time
import os
import json
import logging
from http.server import BaseHTTPRequestHandler
from socketserver import UnixStreamServer

#settings
fan_gpio=12 # Fan GPIO pin number (default: 12)
idle_speed = 25 # Fan speed when under min_temp(IDLE) (min: 0, max: 100)
min_temp = 45 # Fan starting temperature
max_temp = 60 # Fan max speed temperature (max: 60)
socket_path = '/run/kvmd/fan.sock' # UNIX socket path

#define GPIO
RPi.GPIO.setwarnings(False)
RPi.GPIO.setmode(RPi.GPIO.BCM)
RPi.GPIO.setup(fan_gpio, RPi.GPIO.OUT)
pwm = RPi.GPIO.PWM(fan_gpio,100)
running = False

# setup logging
logging.basicConfig(level=logging.INFO)

set_speed = None
cpu_temp = None

def calculate_fan_speed(cpu_temp):
    if cpu_temp >= max_temp:
        return 100
    else:
        temp_speed = int((cpu_temp - min_temp) / (max_temp - min_temp) * 100)
        return min(max(idle_speed, temp_speed), 100)

class MyHandler(BaseHTTPRequestHandler):
    def address_string(self):
        try:
            return super().address_string()
        except IndexError:
            return 'unix_socket'

    def do_GET(self):
        logging.info("GET %s", self.path)
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "result": {"version": "1.0"}}).encode())
        elif self.path == '/state':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "result": {"temp": cpu_temp, "fan_speed": set_speed}}).encode())
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not found\n')

#running code
try:
    if os.path.exists(socket_path):
        os.remove(socket_path)
    with UnixStreamServer(socket_path, MyHandler) as httpd:
        os.chmod(socket_path, 0o666)
        while True:
            try:
                with open('/sys/class/thermal/thermal_zone0/temp') as tmpFile:
                    cpu_temp = int(tmpFile.read())/1000
                logging.info("Temp: %s", cpu_temp)
                if cpu_temp >= min_temp:
                    if not running:
                        logging.info("PWM_Start")
                        pwm.start(0)
                        running = True
                    set_speed = calculate_fan_speed(cpu_temp)
                    logging.info("set_speed: %s %%", set_speed)
                    pwm.ChangeDutyCycle(set_speed)
                else:
                    if running:
                        running = False
                        pwm.stop()
                        logging.info("PWM_Stop")
            except Exception as e:
                logging.error("Error: %s", e)

            time.sleep(1)

            httpd.handle_request()

except KeyboardInterrupt:
    pass
pwm.stop()
