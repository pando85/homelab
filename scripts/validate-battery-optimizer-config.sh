#!/usr/bin/env bash
# Validate that battery-optimizer service.yaml preserves production overrides
# when re-vendoring from the app repository template.
#
# Usage: scripts/validate-battery-optimizer-config.sh
#
# This script checks that critical site-specific values are not accidentally
# reverted to template defaults during config re-vendoring.

set -euo pipefail

CONFIG_FILE="apps/battery-optimizer/files/service.yaml"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: Config file not found: $CONFIG_FILE"
    exit 1
fi

ERRORS=0

# Check climate_shadow.enabled
if ! grep -q "^climate_shadow:" "$CONFIG_FILE"; then
    echo "ERROR: climate_shadow section not found"
    ERRORS=$((ERRORS + 1))
elif ! grep -A 1 "^climate_shadow:" "$CONFIG_FILE" | grep -q "enabled: true"; then
    echo "ERROR: climate_shadow.enabled is not 'true' (production override required)"
    echo "       Template default is 'false', but this deployment must have 'enabled: true'"
    ERRORS=$((ERRORS + 1))
fi

# Check climate_shadow.prior
if ! grep -A 5 "^climate_shadow:" "$CONFIG_FILE" | grep -q 'prior: "2311c010392f"'; then
    echo "ERROR: climate_shadow.prior is not '2311c010392f' (site-specific prior required)"
    echo "       Template default is 'null', but this deployment must have a prior ID"
    ERRORS=$((ERRORS + 1))
fi

# Check climate_shadow.optimizer_mode
if ! grep -A 20 "^climate_shadow:" "$CONFIG_FILE" | grep -q 'optimizer_mode: "shadow"'; then
    echo "ERROR: climate_shadow.optimizer_mode is not 'shadow' (advisory mode required)"
    echo "       Template default is 'off', but this deployment must have 'shadow'"
    ERRORS=$((ERRORS + 1))
fi

# Check top-level mode
if ! grep -q "^mode: live" "$CONFIG_FILE"; then
    echo "ERROR: top-level mode is not 'live' (production override required)"
    echo "       Template default is 'shadow', but this deployment must have 'mode: live'"
    ERRORS=$((ERRORS + 1))
fi

# Check the values the 0.16.0 re-vendor silently reset (restored 2026-11-11).  Parsed as YAML,
# so comments and layout do not matter.  full_charge.mode is deliberately not pinned.
PARSE_OUT=$(python3 - "$CONFIG_FILE" <<'PY'
import sys

import yaml

with open(sys.argv[1]) as f:
    cfg = yaml.safe_load(f)

# dotted path -> (required value, template default)
checks = {
    "planning.high_soc_cost_eur_per_kwh_h": (0.01, 0.0),
    "planning.charge_soft_cap_eur_per_kwh": (0.005, 0.0),
    "planning.discharge_margin_bound": (True, False),
    "planning.thermal.mode": ("shadow", "off"),
    "actuation.load_following.enabled": (True, False),
    "actuation.load_following.max_w": (2500, None),
    "full_charge.enabled": (True, False),
}
for path, (want, template) in checks.items():
    node = cfg
    for key in path.split("."):
        node = node.get(key) if isinstance(node, dict) else None
    # type(): True == 1 and 0.01 == 1e-2, but a bool must not match an int or the string "on"
    if node != want or type(node) is not type(want):
        print(f"{path} is {node!r}, expected {want!r} (template default {template!r})")
PY
) || { echo "ERROR: could not parse $CONFIG_FILE (python3 with PyYAML required)"; exit 1; }

if [[ -n "$PARSE_OUT" ]]; then
    while IFS= read -r line; do
        echo "ERROR: $line"
        ERRORS=$((ERRORS + 1))
    done <<< "$PARSE_OUT"
    echo "       Production values reverted by the 0.16.0 re-vendor; see docs/deployment/battery-optimizer.md"
fi

if [[ $ERRORS -gt 0 ]]; then
    echo ""
    echo "Found $ERRORS production override violation(s)."
    echo "See docs/deployment/battery-optimizer.md 'Re-vendoring Config Files' for guidance."
    exit 1
fi

echo "✓ All production overrides preserved"
exit 0
