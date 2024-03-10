# Lilygo Higrow Calibration Guide

## Introduction

Welcome to Lilygo Higrow, a project aimed at providing accurate plant monitoring capabilities through ESP32-based sensors. This README will guide you through the calibration process necessary to ensure precise measurements.

## Home Assistant Integration

To integrate Lilygo Higrow with Home Assistant, follow these steps:

1. Obtain access to the [OpenPlantBook API](https://open.plantbook.io).
2. Install [HACS](https://hacs.xyz/docs/setup/download).
3. Install the [Home Assistant Plantbook integration](https://github.com/Olen/homeassistant-plant).
4. Install the [Home Assistant Plantbook API integration](https://github.com/Olen/home-assistant-openplantbook).
5. Install the [Home Assistant Flower card](https://github.com/Olen/lovelace-flower-card/tree/new_plant).

## Calibration Procedure

To calibrate Lilygo Higrow for accurate measurements, follow these steps:

1. **Modify Settings**: In the `lilygo-higrow.yaml` file, comment out lines marked for calibration and uncomment those marked for adjustment.
2. **Generate Configuration**: Use ESPHOME to generate the configuration file by running the following command:

   ```bash
   make ${YAML_FILE}
   ```

   Ensure your sensor is connected via USB to your PC during this process.

3. **Dry Calibration**: After flashing the file, clean and dry the sensor thoroughly. Take initial measurements from the log output.
4. **Initial Measurement**: Note down the voltage readings for soil conductivity and moisture after 10 minutes of dry run.
5. **Adjust Minimum Values**: Update your YAML file with the minimum values obtained during dry calibration.
6. **Water Calibration**: Submerge the sensor partially in water and record the voltage readings after stabilization.
7. **Adjust Maximum Values**: Update your YAML file with the maximum values obtained during water calibration.
8. **Uncomment Configuration**: Uncomment the previously commented sections in the configuration file.
9. **Reflash**: Once again, flash the updated configuration file using the previous command.
10. **Temperature and Humidity Calibration**: Optionally, calibrate temperature and humidity sensors as per your requirements.

## Conclusion

Congratulations! Your Lilygo Higrow sensor is now calibrated for precise plant monitoring. Enjoy accurate readings and keep your plants healthy and thriving!
