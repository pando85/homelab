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

if [[ $ERRORS -gt 0 ]]; then
    echo ""
    echo "Found $ERRORS production override violation(s)."
    echo "See docs/deployment/battery-optimizer.md 'Re-vendoring Config Files' for guidance."
    exit 1
fi

echo "✓ All production overrides preserved"
exit 0
