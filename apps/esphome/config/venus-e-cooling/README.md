# Venus E V3 Cooling Controller

## Overview

Auxiliary bottom-to-top airflow for the floor-standing **Marstek Venus E V3**
using two existing **Noctua NF-A4x10 FLX** 40 × 40 × 10 mm, 12 V,
three-pin fans. The battery is on the floor, approximately **50 mm** from
the wall. Fans will be mounted horizontally below the rear vertical fins,
blowing upward.

This revision uses the **Sinilink XY-WFMS**: its ESP8266/ESP-12F, 5–36 V
power input and MOSFET switch replace the bare ESP8266MOD, external
buck regulator and separate relay. ESPHome configuration:
[`venus-e-cooling.yaml`](../venus-e-cooling.yaml), using the existing
[`connection.yaml`](../packages/connection.yaml) package.

## Electrical wiring

<div align="center">
  <img src="./wiring.svg" alt="Sinilink XY-WFMS: 12 V supply on VIN+ and VIN−, two Noctua FLX fans on MOSFET-switched OUT+ and OUT− through a Y splitter; unused TACH insulated" width="100%">
</div>

**Power stays on at VIN while only the fan output is switched.**
The XY-WFMS uses a low-side MOSFET: connect fans to **OUT+ and OUT−**.
**Never bridge OUT− to VIN− externally**; that would bypass the switch.
The on-board ESP8266 and MOSFET need no additional GPIO or power wiring.

### Bill of materials

| Qty | Component | Notes |
| --- | --- | --- |
| 1 | **Sinilink XY-WFMS** | ESP8266 / ESP-12F, built-in regulator and MOSFET, 5–36 V input |
| 1 | Regulated **12 V DC / ≥1 A** adapter | Certified external AC/DC supply |
| 1 | DC jack 5.5 × 2.1 mm | As on rack controller; confirm center-positive polarity |
| 2 | Noctua **NF-A4x10 FLX** | 3-pin, 12 V, **0.05 A max per fan** |
| 1 | Noctua **NA-YC1** Y-cable | Both power lines shared; unused TACH insulated |
| 1 | Fan connector breakout/harness | Mate to splitter input, expose **+V and GND only** |
| 1 | Inline low-voltage fuse | Place on incoming positive conductor; insulate wiring |
| 2 | Existing unused Marstek wall-mount bars | Only if geometrically suitable; **not load-bearing** |
| As needed | Brackets, screws, spacers, isolators, enclosure | Measure fit and keep airflow path open |

**No additional ESP8266, MP1584, LM2596, relay module, gate driver
or ESP-12 boot resistors are needed** for the finished installation.

### Power terminals

| From | To | Notes |
| --- | --- | --- |
| Jack **+12 V**, via fuse | XY-WFMS **VIN+** | Permanent 12 V board power |
| Jack **GND** | XY-WFMS **VIN−** | Board supply return |
| XY-WFMS **OUT+** | Y-cable fan **+V** | Noctua pin 2, **red wire** |
| XY-WFMS **OUT−** | Y-cable fan **GND** | Noctua pin 1, **black wire**; switched negative |
| Y-cable **TACH** | **Not connected** | Noctua pin 3, **yellow wire**; insulate separately |

The two fans draw up to **0.10 A total at 12 V**. The 12 V supply also
powers the Sinilink electronics. Check the actual terminal markings
and output polarity with a multimeter before connecting the fans.

### Functional scope

- **ON/OFF:** the Home Assistant **Cooling Fans** entity operates the
  on-board MOSFET via **GPIO4**. GPIO12 is the physical toggle button.
- **Boot behavior:** ESPHome uses `restore_mode: ALWAYS_OFF`; confirm
  actual switch behavior on power-up before relying on it as a hardware
  safety mechanism.
- **No adjustable DC voltage:** the XY-WFMS provides switching, not a
  stable variable-voltage output. We deliberately **do not PWM-chop**
  the supply of these three-pin FLX fans.
- **No RPM measurement:** the module's output switches the low-side
  return; there is no documented ready-to-use tachometer interface
  on this device. A tachometer wire must **not** be attached directly
  to a programming pad, GPIO12 or VIN−. We leave it insulated and
  do not expose an invented RPM entity.

The battery's own thermal protection must remain independent of
Home Assistant or this auxiliary airflow system.

## Initial ESPHome flashing

The XY-WFMS has **six small programming pads at 1.25 mm pitch**.
See the labelled photographs in the
[ESPHome XY-WFMS device page](https://devices.esphome.io/devices/sinilink-xy-vfms/).
Use the existing USB–UART adapter with verified **3.3 V logic**;
thin wires or a pogo-pin jig work.

| Pad on Sinilink | Connect to |
| --- | --- |
| **1 – 3V3** | Reliable regulated **3.3 V** source |
| **2 – RESET** | Momentarily GND for reset (if needed) |
| **3 – GPIO0** | GND **during reset** to enter the serial bootloader |
| **4 – RXD** | USB–UART **TXD** |
| **5 – TXD** | USB–UART **RXD** |
| **6 – GND** | USB–UART / supply **GND** |

**Disconnect the 12 V input during 3V3-pad programming.**
Do not put 5 V on the 3V3 pad, and do not combine two independently
powered 3.3 V sources. If the USB–UART adapter cannot supply enough
current for ESP8266 startup, use a separate regulated 3.3 V supply
(the existing MP1584 can be reused temporarily) and share ground.
Do not use ordinary 2.54 mm Dupont headers against the 1.25 mm pads.

For a local checkout with the required `secrets.yaml` available:

```sh
esphome run apps/esphome/config/venus-e-cooling.yaml \
  --device /dev/ttyUSB0 --upload_speed 115200
```

Alternatively compile/download the binary via the homelab ESPHome
dashboard and flash it locally over UART. Once the module joins Wi-Fi,
later updates can be made OTA through the shared connection package.
The device definition uses `esp8266: board: esp01_1m`.

## Mechanical arrangement

- Fans are oriented **horizontally, blowing upward** into the bottom of
  the rear heatsink's vertical fins; leave the upper outlet clear.
- Each horizontal fan requires **40 mm depth**, not 10 mm; 10 mm is
  its thickness. A 50 mm wall gap leaves little room for brackets and
  inlet air. Check the actual clearance before mounting.
- Reuse the **unused original Marstek wall bars** only as removable fan
  supports, with suitable generic brackets and vibration isolators.
  Do **not** drill, load or modify the battery or its heatsink fins.
- Keep the Sinilink in an insulated, ventilated housing **away from the
  hot exhaust path**, with strain relief and Wi-Fi antenna clearance.

The bracket hole spacing and exact fin entrance have not yet been
measured. Follow the manufacturer's minimum installation clearances.

## Bring-up checklist

1. With everything disconnected, inspect the **VIN+/VIN−/OUT+/OUT−**
   markings, jack polarity, fuse and output wiring.
2. Flash ESPHome at **3.3 V only**, with the 12 V adapter disconnected.
   Release GPIO0, reboot and disconnect the programmer.
3. Power VIN from 12 V. Verify the board connects to Wi-Fi and
   **Cooling Fans** appears in Home Assistant.
4. Confirm **OFF after firmware boot**, then switch ON from HA and the
   on-board button. Both fans must run from OUT+/OUT− at rated speed.
5. Test OFF, power cycling, Wi-Fi disconnect and reconnection.
   Verify that the **fan TACH lead is not wired**.
6. Check upward airflow, fan noise, cable strain relief, actual wall
   clearance and cooling performance under comparable load/ambient
   conditions before leaving the device running unattended.

## References

- [ESPHome Sinilink XY-WFMS](https://devices.esphome.io/devices/sinilink-xy-vfms/)
- [Noctua NF-A4x10 FLX specifications](https://www.noctua.at/en/products/nf-a4x10-flx/specifications)
- [Noctua fan pinout and two-wire operation](https://faqs.noctua.at/en/support/solutions/articles/101000081757)
