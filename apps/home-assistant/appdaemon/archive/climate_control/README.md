# Archived: AppDaemon `climate_control`

Retired when the optimizer took over space heating (ADR-0009 in `pando85/home-energy-optimizer`).
The module and its tests are kept here, outside `apps/`, so AppDaemon does not load them, the
ConfigMap does not mount them and CI does not collect the tests.

To bring the legacy controller back, **revert the switch commit**: it moves both files back under
`apps/`, restores the `climate_control` block of `apps/apps.yaml`, `input_boolean.appdaemon_climate_enable`
and `automation.appdaemon_climate_watchdog`, and removes `climate_actuation.mode: live`. Set
`input_select.climate_optimizer_mode` to `shadow` first. Do not copy files back by hand.
