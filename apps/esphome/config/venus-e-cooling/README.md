# Venus E V3 Cooling Controller

## Overview

Auxiliary **bottom-to-top airflow** for the floor-standing Marstek Venus E V3. This is
a **hardware assembly project**, modeled after the [Rack Controller](../rack-controller/README.md).
The ESPHome configuration is [`../venus-e-cooling.yaml`](../venus-e-cooling.yaml),
using the homelab's existing `packages/connection.yaml` for Wi-Fi, API and OTA.

The battery stands on the floor, with approximately **50 mm between the rear of the
battery and the wall**. Two existing **Noctua NF-A4x10 FLX** 40 × 40 × 10 mm
**three-pin, 12 V** fans will be mounted horizontally near the lower rear heatsink
to encourage upward airflow along its vertical fins. Reuse the **supplied, unused
Marstek wall-mount bars** only after confirming the real hole positions and
clearances; they are **not** carrying the battery's weight in this build.

## Electrical wiring

<div align="center">
  <img src="./wiring.svg" alt="Electrical wiring: 12 V barrel jack feeds both Noctua FLX fans directly through a Y splitter and an MP1584 buck converter for the bare 3.3 V ESP8266MOD; single tachometer to GPIO14" width="100%">
</div>

The **D-SUN MP1584EN is the only buck converter** and supplies regulated
**3.3 V** to the bare ESP8266MOD **and a compatible 3.3 V relay module**.
The relay's **normally-open (NO)** contact switches the **+12 V** fan supply
from the barrel jack to the NA-YC1 splitter. Fan ground remains permanently
connected to common ground. **ESP8266 GPIO5** drives the relay module's
active-high **IN** pin, and **GPIO14** monitors the tachometer of one fan.

**The primary feature is on/off control through Home Assistant**, with
an ESPHome switch named **Cooling Fans**. The fans are off at boot
(`restore_mode: ALWAYS_OFF`); an external **10 kΩ GPIO5 pull-down**
keeps the relay input low while the ESP8266 resets. Confirm the selected
relay really is **active-high** and remains de-energized with the GPIO
disconnected.

The three-pin **FLX** fans do **not** provide variable-speed PWM control.
The ESP8266 can switch both fans simultaneously, but cannot adjust
their RPM or independently control them. Do not PWM-chop the power supply.

### Bill of materials

| Qty | Part | Notes |
| --- | --- | --- |
| 1 | Regulated **12 V DC / 1 A or higher** adapter | 5.5 × 2.1 mm **center-positive** plug; 2 A is also fine |
| 1 | Female 5.5 × 2.1 mm DC jack | Panel mount; check contact polarity with a meter |
| 1 | **D-SUN MP1584EN** adjustable buck (green board) | **12 V → 3.30 V** for the bare ESP8266MOD and relay module |
| 1 | **3.3 V active-high relay module**, single channel, contacts COM/NO | Prefer [BricoGeek PR2004](https://tienda.bricogeek.com/interruptores/2014-modulo-1-rele-33v.html); transistor driver and flyback protection integrated; verify input polarity |
| 1 | **ESP8266MOD / ESP-12** bare Wi-Fi module | Requires 3.3 V supply, boot straps and a carrier/breakout |
| 2 | Noctua **NF-A4x10 FLX** | 40 × 40 × 10 mm; 12 V, **0.05 A max each**, 3-pin |
| 1 | Noctua **NA-YC1** Y-splitter | Both fan power leads, **tach from one fan only** |
| 7 | 10 kΩ resistors | EN, RST, GPIO0, GPIO2 pull-up; GPIO15 and GPIO5 pull-down; tach pull-up |
| 1 each | 100 nF ceramic and 10–47 µF capacitor | Place near ESP8266 VCC/GND on the carrier |
| 1 | ESP-12 breakout/carrier or suitable PCB | ESP module pads are **not** normal 2.54 mm header pins |
| 1 | 3.3 V USB–UART adapter (for programming) | UART TX/RX at 3.3 V logic; never 5 V |
| Optional | FLASH (GPIO0→GND) / RESET (RST→GND) buttons | Useful when programming the bare module |
| 1 | Inline low-voltage fuse, enclosure, wire and insulated terminals | Protect +12 V distribution and provide strain relief |
| 2 | Original Marstek wall-mount bars, if unused | Reuse only if the fit is confirmed; do not drill the battery |
| As needed | Small L-brackets, M3/M4 screws, nuts, spacers, rubber isolators | Exact sizes after physically measuring rail holes |
| Optional | Thin printed PETG fan cradle / deflector | Only if generic brackets do not fit; keep air intake unobstructed |

The fan pair draws **up to 0.10 A at 12 V** (1.2 W at rated voltage);
the ESP8266 and relay module also draw from the 3.3 V MP1584.
Size and test the MP1584 rail for **Wi-Fi bursts plus the relay-coil load**;
verify the 3.3 V rail does not sag on fan switching or Wi-Fi connection.
A quality **12 V / 1 A** supply should leave headroom, subject to
measuring the actual relay and ESP current.
Do not interpret the adapter's current rating as forced current.

### Power and signal connections

| From | To | Function |
| --- | --- | --- |
| DC jack +12 V | MP1584 **IN+** and relay **COM** contact | Fused 12 V feed for controller and switched fans |
| Relay **NO** contact | Y-cable fan **+V** (3-pin connector pin 2) | 12 V delivered only while relay energized; **NC unused** |
| DC jack GND | MP1584 **IN−**, Y-cable fan **GND** (pin 1) | Common return for fans and ESP8266 |
| MP1584 **OUT+** (measured **3.30 V**) | ESP8266 **VCC**, relay module **VCC** | Use 3.3 V relay module, **not** a 5 V / 12 V coil board |
| MP1584 **OUT−** | ESP8266 **GND**, relay module **GND**, Y-cable fan **GND** (pin 1) | Shared ground; **do not switch GND** |
| ESP8266 **GPIO5** | Relay module **IN** | Active-HIGH enable; **10 kΩ pull-down to GND** for boot OFF |
| Y-cable single **TACH** (pin 3) | ESP8266 **GPIO14** | One fan RPM only; **10 kΩ pull-up to 3.3 V**, no 12 V on GPIO |
| ESP8266 **EN**, **RST**, **GPIO0**, **GPIO2** | Individual **10 kΩ** pull-ups to 3.3 V | Normal boot and enable |
| ESP8266 **GPIO15** | **10 kΩ** pull-down to GND | Required normal boot strap |
| UART TX/RX on ESP8266 | 3.3 V USB–UART RX/TX, crossed | Only needed to flash/diagnose; shared GND |
| GPIO0 → GND during reset | Temporary FLASH button / jumper | Enter serial bootloader; release for normal boot |

**Do not parallel two tachometer outputs.** The NA-YC1 splitter provides
a single tach signal by design. Most Noctua PC fan tachometer outputs are
open-collector; use a **3.3 V pull-up** rather than a 5 V or 12 V pull-up,
and confirm the actual signal on your hardware. Typical PC fans generate
**two tach pulses per revolution** (RPM = pulses/s × 30).

The MP1584 is the **only** adjustable converter. Set its output to
**3.30 V** and verify polarity **before connecting the ESP8266**.
The fan harness connects to the relay's **NO contact**, **not** to the
MP1584 output. The MP1584 feeds the ESP8266 and 3.3 V relay module only.
Do not run fan current through GPIO5. Verify that NO is open while off
and closes onto COM only while the relay is energized.
The ESP8266 needs nearby decoupling and a reliable 3.3 V rail.
Keep all low-voltage grounds common. The MP1584 output is **not
controllable from firmware** in this configuration.

## Mechanical arrangement

- **Airflow:** Both 40 mm fans lie **horizontally**, axes pointing upward,
  near the **bottom of the rear vertical-fin heatsink**. A small open-sided
  plenum may help feed the lower fins; air must escape **above** the battery.
- **50 mm wall gap:** The **40 mm footprint** of each fan must fit the rear
  gap **plus its fasteners and clearance**. Its 10 mm thickness is vertical.
  Do **not** confuse the 10 mm fan thickness with the 40 mm depth of the
  horizontally positioned fan.
- **Battery on the floor:** Keep the lower intake open to room air; the
  floor and skirting board can block an unraised intake. Verify the
  bottom of the fins is accessible and the fans **do not cover existing
  ventilation, connectors or drain paths**.
- **Original wall bars:** If they are unused, try them as a **removable
  support frame** with standard L-brackets and screw-on fan corners.
  Do not borrow load-bearing fasteners, rely on unmeasured hole spacing,
  or transfer vibration/weight into heatsink fins.
- **Enclosure:** Locate the ESP8266, relay and MP1584 **outside the hot
  rear air channel**, with cable strain relief and insulated electronics.
  The ESP antenna should remain clear of metal profiles.

The Marstek mounting-bar dimensions and exact fin access have **not been
measured on this installation**. Cut length, bracket count and hole spacing
are intentionally left adjustable rather than inventing a drilling template.
Confirm the applicable Marstek installation-clearance requirements: an
auxiliary fan does **not** replace the manufacturer's minimum wall spacing.

## Bring-up checklist

1. Check jack **polarity** and verify the PSU's 12 V output.
2. Set the **green MP1584 to 3.30 V** with a multimeter **before**
   connecting the ESP8266. Check that the separate fan branch measures
   **12 V**, and that all returns share a common ground.
3. Verify the ESP-12 carrier's boot straps (EN/RST/GPIO0/GPIO2 high;
   GPIO15 low), **GPIO5 relay pulldown**, 3.3 V decoupling and GPIO14 tach pull-up.
4. Energize the ESP8266 **without** the fan harness first; check stable
   boot and absence of 5 V / 12 V on GPIOs.
5. With the relay **off**, verify **COM = 12 V**, **NO unpowered**,
   both fans stopped and the ESP8266 still powered at 3.3 V. Then enable
   `Cooling Fans` and verify both fans start at **12 V**, one tachometer
   reports RPM, and the 3.3 V supply remains stable.
6. Reset the ESP8266 and check the relay remains **off throughout boot**
   thanks to the external GPIO5 pulldown. Check both on/off operations,
   correct airflow and no blocked intakes. Measure thermal improvement
   at matched battery load and ambient temperature.

## ESPHome firmware

The [`venus-e-cooling.yaml`](../venus-e-cooling.yaml) configuration uses the
existing shared [connection package](../packages/connection.yaml). It targets
`esp8266: board: esp12e` and exposes **Cooling Fans** as an active-high
GPIO5 relay switch, defaulting to **ALWAYS_OFF** on reboot.
It also exposes **Fan 1 Speed** from GPIO14 using `pulse_counter` and
a ×0.5 filter (two tachometer pulses per revolution), Wi-Fi diagnostics,
and controller status. The Y-cable reports only one fan's tachometer.
Home Assistant can enable the relay explicitly or automate it against the
battery's existing temperature sensor; the battery remains passively cooled
if the auxiliary system is unavailable. Keep thermal safety limits and
manufacturer protections independent of Home Assistant.

### Initial serial flash (bare ESP-12)

A bare ESP8266MOD has **no USB port**. Choose either a compatible **ESP-12
USB programmer with a socket** (before soldering the module) or a 3.3 V
USB–UART adapter wired to a carrier with boot resistors. The latter also
allows serial recovery after assembly:

| USB–UART / programming header | Bare ESP-12 |
| --- | --- |
| Adapter TX (3.3 V logic) | RXD0 |
| Adapter RX (3.3 V logic) | TXD0 |
| Adapter GND | GND (shared with MP1584) |
| Supply for ESP | MP1584 OUT+ at **3.30 V**, not USB 5 V |
| FLASH button / jumper | GPIO0 → GND during reset |
| RESET button / jumper | RST → GND momentarily |

Keep **GPIO15 low**, **GPIO2 high** and **EN high**. For normal boot,
GPIO0 must be high; for serial download, hold **GPIO0 low while
resetting**. Use a dedicated MP1584 to supply the module during serial
flashing; **do not connect USB–UART VCC to the MP1584's output** unless
a deliberately designed single-supply arrangement is used.

When the same ESPHome configuration and a valid local
`secrets.yaml` are available on the flashing computer, the initial
upload can be performed with:

```bash
esphome run apps/esphome/config/venus-e-cooling.yaml \
  --device /dev/ttyUSB0 --upload_speed 115200
```

The homelab deploys connection secrets through Vault in Kubernetes;
`secrets.yaml` is not committed to Git. Alternatively compile/download
the firmware through the already configured ESPHome dashboard, then
flash the resulting ESP8266 binary locally using a suitable serial
programmer. After the first successful Wi-Fi/API/OTA boot, further
updates can be applied over the network using the existing dashboard.

References: [Espressif ESP8266 boot modes](https://docs.espressif.com/projects/esptool/en/latest/esp8266/advanced-topics/boot-mode-selection.html)
and [ESPHome command-line installation](https://esphome.io/guides/cli/).

