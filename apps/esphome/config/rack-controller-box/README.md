# Rack Controller

## Overview

Welcome to the Temperature Rack Controller README. This documentation guides you through assembling
and utilizing our controller for precision temperature management in rack-mounted environments.

## Components

- **Noctua NF-S12A PWM Fans:** Efficient and low-noise fans for optimal cooling.

- **ESP-WROOM-32 Microcontroller:** Powerful controller with Wi-Fi connectivity for remote
  monitoring.

- **OLED Display (128x64, I2C SSD1306):** High-resolution display for real-time temperature and
  system status.

- **Temperature Sensor (DHT22):** Accurate measurement of ambient temperature and humidity.

- **DC Power Female Jack (5.5x2.1mm):** Connector for powering the ESP32 and fans, ensuring a reliable power source.

- **DC-DC Step-down (LM2596):** Voltage regulation component that efficiently steps down the voltage, contributing to stable and reliable operation of the ESP32 controller.

## Functionality

- **Home Assistant Integration:** Effortlessly manage and monitor sensors through Home Assistant
  control.

- **Fan Control:** Enjoy automated Noctua fan control using PID based on temperature readings, or
  opt for manual configuration with a specific speed.

- **Display Interface:** Experience real-time monitoring through the user-friendly OLED display.

## Images

<div style="display: flex; justify-content: space-between;">
  <img src="https://raw.githubusercontent.com/pando85/homelab/master/apps/esphome/config/rack-controller-box/rack-controller-front.jpg" width="45%" height="auto" />
  <img src="https://raw.githubusercontent.com/pando85/homelab/master/apps/esphome/config/rack-controller-box/rack-controller-back.jpg" width="40%" height="auto" />
</div>
<div style="display: flex; justify-content: center;">
  <img src="https://raw.githubusercontent.com/pando85/homelab/master/apps/esphome/config/rack-controller-box/rack-controller.jpg" width="50%" height="auto" />
</div>
