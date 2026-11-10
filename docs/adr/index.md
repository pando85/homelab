# Architecture decisions

The battery optimizer ADRs moved to
[pando85/home-energy-optimizer](https://github.com/pando85/home-energy-optimizer), together with the
application code. Homelab keeps only deployment concerns; its own ADRs live here.

- [ADR-0005: Gateway API with Envoy Gateway instead of ingress-nginx](0005-gateway-api-with-envoy-gateway.md)

Battery optimizer ADRs (in `pando85/home-energy-optimizer`):

- [ADR-0001: Home battery economic dispatch](https://github.com/pando85/home-energy-optimizer/blob/main/docs/adr/0001-home-battery-economic-dispatch.md)
- [ADR-0002: Appliance-aware load forecasting](https://github.com/pando85/home-energy-optimizer/blob/main/docs/adr/0002-appliance-aware-load-forecasting.md)
- [ADR-0003: Phase 1 measurement and baseline](https://github.com/pando85/home-energy-optimizer/blob/main/docs/adr/0003-phase1-measurement-and-baseline.md)
- [ADR-0004: Battery optimizer production architecture](https://github.com/pando85/home-energy-optimizer/blob/main/docs/adr/0004-battery-optimizer-production-architecture.md)
