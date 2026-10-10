# Temperature Alert Thresholds for k8s-amd64-1

## Problem

The `HighTemperature` alert was firing during normal CI workloads on k8s-amd64-1, reporting CPU temperatures of 88-89°C as critical. This created alert fatigue since these spikes were brief (15-20 minutes) and part of expected behavior during intensive builds.

## Root Cause

The AMD Ryzen 5 5500U on k8s-amd64-1 has:
- **TjMax (max junction temp):** 105°C
- **Thermal throttling:** begins ~100°C
- **Idle temperature:** ~58°C
- **CI workload spikes:** up to 98°C (brief, P95 is only 63°C)

The old thresholds (warning: 85°C, critical: 90°C) were too conservative for a node that legitimately reaches 98°C during CI bursts. 96% of readings stay below 65°C; only ~2% exceed 85°C during legitimate workload spikes.

## How to Diagnose

Check current temperatures:
```bash
ssh k8s-amd64-1 "for d in /sys/class/hwmon/hwmon*/; do echo \"== \$(cat \${d}name 2>/dev/null) ==\"; for f in \"\${d}\"temp*_input; do [ -f \"\$f\" ] && echo \"\$(basename \$f): \$(cat \$f)\"; done; done"
```

Query Prometheus for historical data:
```promql
node_hwmon_temp_celsius{instance="k8s-amd64-1", chip="pci0000:00_0000:00:18_3"}
```

Check CPU model and specs:
```bash
ssh k8s-amd64-1 "lscpu | grep 'Model name\|CPU max MHz'"
```

## Fix / Workaround

Updated thresholds in `system/monitoring/resources/temperature-prometheus-rules.yaml`:

| Severity | Chip | Threshold |
|----------|------|-----------|
| Warning | CPU (pci0000:00_0000:00:18_3) | 92°C |
| Warning | Other chips (NIC, GPU, etc.) | 70°C |
| Critical | CPU | 97°C |
| Critical | Other chips | 75°C |

**Rationale:**
- **Warning at 92°C:** Sustained high temperature that indicates a problem (cooling issue, thermal paste degradation, or extreme workload). Brief CI spikes to 98°C won't trigger because they're transient and the `for: 5m` duration filters them.
- **Critical at 97°C:** Imminent thermal throttling (3°C from 100°C). This is a real problem requiring immediate attention.
- **Other chips at 70/75°C:** NIC (r8169) typically runs 67°C, GPU (amdgpu) 72-77°C. Thresholds set above normal operating range but below dangerous levels.

## Key Insights

1. **P95 vs peak matters:** CPU P95 is 63°C but peaks reach 98°C. Alerting on peaks without considering duration causes false positives.
2. **`for: 5m` filters transient spikes:** The 5-minute duration means brief CI bursts (< 5 min at high temp) won't trigger alerts.
3. **Chip-specific thresholds are necessary:** Different sensors report different chips with different thermal characteristics. The CPU (k10temp) can safely run hotter than NICs or GPUs.
4. **Know your hardware limits:** Always check TjMax before setting thresholds. For AMD Ryzen 5 5500U, anything below 95°C is within spec for sustained loads.

## Related

- Alert rule: `system/monitoring/resources/temperature-prometheus-rules.yaml`
- Temperature dashboard: `system/monitoring/resources/temperature-dashboard-configmap.yaml`
