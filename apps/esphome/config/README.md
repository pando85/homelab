# ESPHome Device Configuration

Operational reference for managing IoT devices in the homelab. All device configs live in this
directory and are deployed via GitOps to an ESPHome pod running in Kubernetes.

## Architecture

```
Device YAML + Packages ──► Helm chart ──► ConfigMap (config) + Secret (secrets)
                                                      │
                                                  ESPHome Pod
                                                  (initContainer copies to /config)
                                                      │
                                                   OTA flash ──► Physical device
```

**How it works:**

1. Device YAML files (`*.yaml` in this dir) and packages (`packages/*.yaml`) are bundled into
   Kubernetes ConfigMaps by the Helm chart (`templates/configmap.yaml`,
   `templates/configmap-packages.yaml`).
2. Secrets (WiFi, OTA, API keys) are fetched from Vault via ExternalSecret
   (`templates/external-secret.yaml`) and assembled into a `secrets.yaml` file inside a Secret.
3. The ESPHome pod mounts both as a **projected volume** at `/original/config/`. An initContainer
   copies everything to a writable emptyDir at `/config/` (this also makes devices show as "online"
   in the dashboard on startup).
4. The ESPHome dashboard at `esphome.internal.grigri.cloud` compiles and OTA-flashes firmware to
   devices.
5. The pod runs on node `prusik` (amd64) for faster builds.

**Important:** Config files in this directory are the source of truth. Edits made via the ESPHome
web UI are ephemeral — they will be overwritten on next ArgoCD sync.

## Device Catalog

| Device | File | Platform | Type | Location / Purpose |
|--------|------|----------|------|--------------------|
| Garage Door Opener | `garage-door-opener.yaml` | ESP32 | RF transceiver | Garage door via CC1101 (433.92 MHz) |
| Irrigation Controller Corner | `irrigation-controller-corner.yaml` | ESP32 | Sprinkler | Corner garden zone, 2 valves |
| Irrigation Controller Mid | `irrigation-controller-mid.yaml` | ESP32 | Sprinkler | Mid garden zone, 2 valves |
| Lilygo Higrow 1 | `lilygo-higrow-1.yaml` | ESP32 | Plant sensor | Plant monitoring station 1 |
| Lilygo Higrow 2 | `lilygo-higrow-2.yaml` | ESP32 | Plant sensor | Plant monitoring station 2 |
| Mini Switch 1 | `mini-switch-1.yaml` | ESP8266 | Light switch | Smart light switch |
| Mini Switch 2 | `mini-switch-2.yaml` | ESP8266 | Light switch | Smart light switch |
| Mini Switch 3 | `mini-switch-3.yaml` | ESP8266 | Light switch | Smart light switch |
| Mini Switch 4 | `mini-switch-4.yaml` | ESP8266 | Light switch | Smart light switch |
| Mini Switch Office A | `mini-switch-office-a.yaml` | ESP8266 | Light switch | Office smart light switch |
| Rack Controller | `rack-controller.yaml` | ESP32 | Fan controller | Server rack cooling + OLED display |
| Smart Plug Snapmaker | `smart-plug-v2-snapmaker.yaml` | ESP8266 | Power meter | Snapmaker 3D printer plug |
| Smart Plug Terrace | `smart-plug-v2-terrace.yaml` | ESP8266 | Power meter | Terrace power plug |

## Package System

Devices use `!include` to pull in shared packages. Each device YAML only defines `substitutions`
and a list of packages. All logic lives in the packages.

### Package Reference

| Package | File | Used by | Controls |
|---------|------|---------|----------|
| connection | `packages/connection.yaml` | All devices | WiFi (with fallback AP), OTA, API encryption, mDNS, SNTP, captive portal |
| irrigation-controller | `packages/irrigation-controller.yaml` | irrigation-controller-{corner,mid} | ESP32 board, relays, sprinkler logic, scheduling, web server |
| lilygo-higrow | `packages/lilygo-higrow.yaml` | lilygo-higrow-{1,2} | ESP32 board, DHT11, soil moisture ADC, conductivity ADC, BH1750 lux, water pump |
| mini-switch | `packages/mini-switch.yaml` | mini-switch-{1..4,office-a} | ESP8285 board, relay, status LED, wired switch, power button, multi-click |
| smart-plug-v2 | `packages/smart-plug-v2.yaml` | smart-plug-v2-{snapmaker,terrace} | ESP8285 board, CSE7766 power meter (UART), relay, energy tracking |
| cc1101 | `packages/cc1101.yaml` + `packages/cc1101.h` | garage-door-opener | CC1101 RF transceiver driver (SPI), 433.92 MHz, raw TX/RX |

### How Substitutions Work

Packages use `${variable}` placeholders. Device YAML files set them via `substitutions:`. Common
substitutions:

- `name` — ESPHome device identifier (must be unique, used as hostname)
- `friendly_name` — Human-readable name shown in Home Assistant
- `project_name` / `project_version` — Metadata for ESPHome dashboard import

Device-type-specific substitutions:

| Substitution | Package | Purpose |
|---|---|---|
| `id_prefix` | irrigation-controller | Prefix for entity IDs to avoid collisions between controllers |
| `light_restore_mode` | mini-switch | Restore behavior: `RESTORE_DEFAULT_OFF`, `RESTORE_DEFAULT_ON`, etc. |
| `relay_restore_mode` | smart-plug-v2 | Restore behavior for relay on power loss |
| `update_interval` | lilygo-higrow | Sensor read interval (e.g. `1min`) |
| `moisture_min` / `moisture_max` | lilygo-higrow | ADC calibration: dry voltage → 0%, wet voltage → 100% |
| `conductivity_min` / `conductivity_max` | lilygo-higrow | ADC calibration for fertilizer sensor |

### Where to Make Changes

| Change | Edit |
|--------|------|
| Per-device setting (name, calibration, restore mode) | Device YAML file |
| Shared behavior for all devices of a type | Package YAML file |
| WiFi, OTA, API config for all devices | `packages/connection.yaml` |
| Adding a new shared feature across device types | New package file, add `!include` to relevant device YAMLs |

## GPIO Pin Assignments

### Garage Door Opener (CC1101 on ESP32)

| Pin | Function | Notes |
|-----|----------|-------|
| GPIO18 | SPI SCK | CC1101 clock |
| GPIO19 | SPI MISO | CC1101 data out |
| GPIO23 | SPI MOSI | CC1101 data in |
| GPIO5 | SPI CSN | CC1101 chip select |
| GPIO32 | GDO0 | RF transmit |
| GPIO33 | GDO2 | RF receive |

### Irrigation Controller (ESP32)

| Pin | Function | Notes |
|-----|----------|-------|
| GPIO16 | Relay 1 | Zone 1 valve |
| GPIO17 | Relay 2 | Zone 2 valve |
| GPIO23 | LED | Status LED (on when any relay active) |

### Lilygo Higrow (ESP32, lolin_d32 board)

| Pin | Function | Notes |
|-----|----------|-------|
| GPIO4 | Sensor power | Pullup, always-on, internal |
| GPIO16 | DHT11 | Temperature + humidity |
| GPIO19 | Water pump | Optional pump switch |
| GPIO25 | I2C SDA | BH1750 lux sensor |
| GPIO26 | I2C SCL | BH1750 lux sensor |
| GPIO32 | ADC | Soil moisture (capacitive) |
| GPIO34 | ADC | Soil conductivity (fertilizer) |

### Mini Switch (ESP8266, esp8285 board)

| Pin | Function | Notes |
|-----|----------|-------|
| GPIO4 | Status LED | Inverted (active low) |
| GPIO13 | Relay output | Controls the light/relay |
| GPIO14 | Wired switch | Input with pullup, 100ms debounce |
| GPIO3 | Power button | Inverted, input pullup |

**Multi-click actions:**
- Wired switch: 7 rapid clicks → factory reset
- Power button: short press → toggle relay; 4s hold → factory reset

### Rack Controller (ESP32)

| Pin | Function | Notes |
|-----|----------|-------|
| GPIO16 | Fan PWM (LEDc) | 25 kHz per Noctua specs |
| GPIO17 | Fan tachometer | Pulse counter, ×0.5 multiplier for RPM |
| GPIO18 | I2C SCL | OLED display (SSD1306) |
| GPIO19 | I2C SDA | OLED display (SSD1306) |
| GPIO25 | DHT22 | Temperature + humidity sensor |

### Smart Plug V2 (ESP8266, esp8285 board)

| Pin | Function | Notes |
|-----|----------|-------|
| GPIO5 | Power button | Input pullup, inverted |
| GPIO12 | Relay | Main power switch |
| GPIO13 | Status LED | Inverted (active low) |
| RX | UART (CSE7766) | Power meter IC, 4800 baud, even parity |

**Power button actions:** short press → toggle relay; 4s hold → factory reset

## Common Operations

### Add a New Device

1. Identify the device type (see catalog above).
2. Copy an existing device YAML of the same type as a template.
3. Update `substitutions`:
   - `name` — unique identifier (lowercase, hyphens)
   - `friendly_name` — display name
   - `project_name` — follows `<vendor>.<device-type>-<n>` convention
   - Any calibration or mode substitutions specific to the device type
4. If the device uses a **new hardware type**, create a new package in `packages/`.
5. Commit — ArgoCD will sync the ConfigMap. The device will appear in the ESPHome dashboard.
6. Flash via the ESPHome dashboard (first flash requires USB/serial; subsequent flashes are OTA).

### Update Device Firmware (OTA)

From this directory with direnv active:

```bash
# Install ESPHome CLI + dependencies
make requirements

# Pull secrets from Kubernetes (requires kubectl access to grigri context)
make secrets

# Compile and OTA-flash a specific device
make <device-name>.yaml
# e.g.: make rack-controller.yaml
```

This runs `esphome run <device>.yaml` which compiles firmware and pushes it over OTA.

### Read Device Logs

```bash
make logs FILE='<device-name>.yaml'
# e.g.: make logs FILE='rack-controller.yaml'
```

### Run Local Dashboard

For development without the Kubernetes pod:

```bash
make local-dashboard
# Opens ESPHome dashboard at http://localhost:6052
```

### Update Secrets

Secrets are stored in Vault and synced via ExternalSecret. The `secrets.yaml` file on disk is
generated locally for CLI use:

```bash
make secrets
```

This pulls from `kubectl --context=grigri -n esphome get secret esphome-secrets`. **Never commit
`secrets.yaml`** — it is gitignored.

To change actual secret values, update them in Vault at these paths:

| Vault Path | Key | Used for |
|---|---|---|
| `/esphome/wifi` | `ssid`, `password`, `fallback_ap_password` | WiFi credentials (all devices) |
| `/esphome/ota` | `password` | OTA flash authentication |
| `/esphome/home-assistant` | `api_key` | ESPHome ↔ Home Assistant API encryption |
| `/esphome/web-server` | `username`, `password` | Device web server auth |

### Change WiFi or Network Settings

Edit `packages/connection.yaml`. All devices include this package. Changes apply to every device
after next OTA flash.

Current network config:
- WiFi SSID and password from secrets
- Device domain: `.iot.grigri`
- SNTP server: `pfsense.grigri`
- Fallback AP mode: activates after 5 min if WiFi lost, reboots after 3h without connection
- Power save: disabled (`none`)

### Calibrate Lilygo Higrow Sensors

See the detailed calibration guide: [`packages/lilygo-higrow.md`](packages/lilygo-higrow.md)

Quick summary:
1. Comment out calibration filters, uncomment raw voltage output in `packages/lilygo-higrow.yaml`
2. Flash and run dry → record `moisture_min` and `conductivity_min` voltages from logs
3. Submerge in water → record `moisture_max` and `conductivity_max` voltages
4. Update substitution values in device YAML (e.g. `lilygo-higrow-1.yaml`)
5. Uncomment calibration filters, reflash

Temperature/humidity offsets (DHT11) are hardcoded in the package:
- Temperature: `-3.1` °C offset
- Humidity: `+11` % offset

### Tune Rack Controller PID

The rack controller uses a PID climate controller with autotune support.

**Current PID values** (in `rack-controller.yaml`):
- `kp: 1.09135`, `ki: 0.00463`, `kd: 64.31172`
- Default target: 27 °C
- Deadband: ±1 °C (reduces oscillation near target)

**To autotune:**
1. Press the "PID Climate Autotune" button in Home Assistant or ESPHome dashboard.
2. The controller will oscillate the fan to learn system dynamics.
3. New PID values are logged — copy them into `rack-controller.yaml` and reflash.

**Fan specs:** Noctua NF-S12A PWM, 25 kHz PWM frequency, max ~1200 RPM (tachometer ×0.5).

### Modify Irrigation Schedule

The irrigation controllers have built-in scheduling logic in `packages/irrigation-controller.yaml`:

- **Day-of-week switches:** Sunday through Saturday toggle switches (default: all enabled)
- **Start time:** Configurable via `Schedule Start Time` datetime entity
- **Schedule enable:** `Enable Schedule` toggle switch
- **Zone durations:** Per-zone run duration (1–60 min, default 8 min)
- **Multiplier:** Speed up/slow down entire cycle (0.1×–5×)

Schedule checking runs every minute via Home Assistant time sync. Auto-advance is enabled by
default (Zone 1 → Zone 2 sequentially).

### Modify Garage Door RF Codes

RF codes are in `packages/cc1101.yaml`. The CC1101 operates at 433.92 MHz with 200 kHz bandwidth.

- **Received codes** are defined as `binary_sensor` entries with `raw` code matchers
- **Transmitted codes** are defined as `button` entries that call `beginTransmission()` →
  `transmit_raw` → `endTransmission()`

To capture new codes, check the ESPHome logs while triggering the original remote — raw codes are
dumped by the `remote_receiver` component.

## Device Type Deep Dives

### Garage Door Opener

Custom ESP32 + CC1101 RF transceiver. Uses a C++ driver (`packages/cc1101.h`) wrapping the
SmartRC-CC1101-Driver-Lib via SPI. Provides both transmit and receive on 433.92 MHz. The CC1101
switches between TX (GDO0 as output) and RX (GDO2 as input) modes. RSSI is exposed as a sensor.

Hardware references: [esphome-cc1101](https://github.com/dbuezas/esphome-cc1101)

### Irrigation Controller

ESP32 relay board with 2 GPIO-controlled relays driving motorized ball valves on 220V. Uses
ESPHome's built-in `sprinkler` component for zone sequencing. Each controller manages 2 zones with
independent duration and enable toggles. The web server provides a local control interface (auth
via secrets). Hardware photos and assembly: [`irrigation-controller/README.md`](irrigation-controller/README.md)

### Lilygo Higrow

LILYGO T-Higrow ESP32 plant sensor with capacitive soil moisture, conductivity, DHT11 temp/humidity,
and BH1750 ambient light. Has an optional water pump on GPIO19 and a sensor power switch on GPIO4.
Supports deep sleep (commented out by default — set `run_duration` and `sleep_duration` substitutions
to enable). Each unit requires individual ADC calibration (see calibration guide).

Home Assistant integration requires: OpenPlantBook API, HACS, Plant integration, Flower card.

### Mini Switch

Athom Mini Switch (ESP8266/esp8285). Single relay controlled as a `light` entity. Supports both a
wired wall switch (GPIO14) and an on-device button (GPIO3) with multi-click patterns. State is
persisted to flash (`restore_from_flash: true`). Factory reset via 7 rapid clicks on wired switch
or 4s hold on button. Logger baud rate set to 0 to free UART for GPIO3.

### Rack Controller

Custom ESP32 with PID-controlled Noctua fan cooling, DHT22 temp/humidity sensor, and SSD1306 OLED
display. The display shows current temperature and fan RPM. Uses a template output to arbitrate
between PID auto mode and manual fan speed. The PID controller targets 27 °C with a ±1 °C deadband.
Fan PWM runs at 25 kHz per Noctua specs, RPM measured via pulse counter on GPIO17 with ×0.5 filter.

Hardware photos and assembly: [`rack-controller/README.md`](rack-controller/README.md)

### Smart Plug V2

Athom Smart Plug V2 (ESP8266/esp8285) with CSE7766 power metering IC over UART. Tracks current,
voltage, power, energy, apparent power, and power factor. Total energy is accumulated in a global
variable persisted to flash. Daily energy is tracked via `total_daily_energy`. Readings below 3W /
60mA are zeroed to suppress standby noise. Relay defaults to off on power loss.
