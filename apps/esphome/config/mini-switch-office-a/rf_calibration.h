// Force a full RF calibration on every boot of this ESP8266.
//
// By default the SDK only does a full calibration on power-on and reuses the stored data after
// a software reset. On this unit the radio goes silent minutes after power-on while the Wi-Fi
// stack still reports "connected", and a software reboot does not bring it back; only a power
// cycle does. Powerup option 3 makes every boot do the full calibration (about 200 ms longer).
// See docs/troubleshooting/office-iot-wifi-flapping.md.
//
// RF_PRE_INIT() defines the Arduino core's weak __run_user_rf_pre_init() hook (Esp.h), which
// the core calls from user_rf_pre_init() before the RF is initialised.
#pragma once

#include <Arduino.h>
#include <user_interface.h>

RF_PRE_INIT() {
  system_phy_set_powerup_option(3);
}
