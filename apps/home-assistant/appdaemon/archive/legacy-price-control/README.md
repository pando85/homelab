# Legacy price-driven heat-pump control (archived)

`climate_control` (cheapest-hours space heating) and `dhw_control` (DHW price forcing), their
tests, their `apps.yaml` entries and their Home Assistant packages, as they ran until the comfort
controller replaced them. Nothing here is deployed: the AppDaemon and Home Assistant ConfigMaps only
glob the top level of `appdaemon/apps/` and `config/integrations/`.

Rollback is `git revert` of the comfort-controller commits, not a copy from this directory. See
`docs/user-guide/heat-pump-comfort-controller.md`.
