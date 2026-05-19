# Irrigation Controller

## Overview

ESP32 relay board with 2 GPIO-controlled relays driving motorized ball valves on 220V. Uses
ESPHome's built-in `sprinkler` component for zone sequencing with scheduling via Home Assistant
time sync.

## Components

- **Motorized ball valve powered by 220V:** Efficient and reliable valve for controlling water flow.
- **ESP32 Microcontroller two relays module powered by 220V:** Powerful controller with Wi-Fi
  connectivity for remote control.

## Functionality

- **Home Assistant Integration:** Effortlessly manage and monitor sensors through Home Assistant
  control.
- **Zone sequencing:** Auto-advances from Zone 1 to Zone 2 with configurable durations.
- **Day-of-week scheduling:** Toggle switches for each day, configurable start time.
- **Multiplier:** Speed up/slow down entire cycle (0.1x to 5x).

## Images

<div style="display: flex; justify-content: space-between;">
  <img src="https://raw.githubusercontent.com/pando85/homelab/master/apps/esphome/config/irrigation-controller/esp32.jpg" width="45%" height="auto" />
  <img src="https://raw.githubusercontent.com/pando85/homelab/master/apps/esphome/config/irrigation-controller/esp32-box.jpg" width="40%" height="auto" />
</div>
<div style="display: flex; justify-content: center;">
  <img src="https://raw.githubusercontent.com/pando85/homelab/master/apps/esphome/config/irrigation-controller/esp32_with_valve.jpg" width="50%" height="auto" />
</div>

## Troubleshooting

### WiFi Disconnects (irrigation-controller-mid)

**Symptom:** irrigation-controller-mid disconnects multiple times per day and reconnects on its own.
irrigation-controller-corner (identical hardware and config) is stable.

**Root cause:** Weak WiFi signal. The mid controller has a wall between it and the AP, resulting in
marginal signal strength.

**Measured WiFi signal comparison (from Home Assistant database):**

| Device | Avg Signal | Worst | Typical Daily Readings (hourly) |
|--------|-----------|-------|-------------------------------|
| irrigation-controller-corner | -74 to -76 dBm | -85 dBm | 24/24 (100%) |
| irrigation-controller-mid | -81 to -86 dBm | -93 dBm | 12-24 (varies, gaps = offline) |

Below -85 dBm ESP32 WiFi becomes unreliable. Mid frequently drops to -93 dBm.

**Config tuning applied** (commit `322dbc01`, Oct 2025):

| Setting | Value | Purpose |
|---------|-------|---------|
| `fast_connect` | `false` | Scan for best AP signal |
| `power_save_mode` | `none` | Disable WiFi power saving |
| `reboot_timeout` | `3h` | Force reboot if WiFi lost for 3h |
| `ap_timeout` | `5min` | Enter fallback AP quickly |

These settings are optimal for weak-signal scenarios. No further software tuning will help.

**Note on historical data:** The HA `statistics` table shows flat signal values (e.g., exactly
-85.0 dBm for mid, -77.0 for corner) for periods where raw state data has been purged. This is HA's
statistics aggregation, not a stuck sensor. Don't rely on the long-term statistics table for signal
variation analysis — check the `states` table or short-term statistics instead.

**Physical fixes (in order of effort/impact):**

1. Rotate/reposition the ESP32 board — PCB antenna is directional, even 90 degree rotation can
   change signal by 5-10 dBm
2. Add a WiFi repeater on the same side of the wall as mid
3. Use an ESP32 board with external antenna connector (IPEX/U.FL) + 2.4 GHz antenna
