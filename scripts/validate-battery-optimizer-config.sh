#!/usr/bin/env bash
# Validate the battery-optimizer site overrides file.
#
# Usage: scripts/validate-battery-optimizer-config.sh
#
# The image ships the base configs (/app/config); the homelab holds only the sparse
# apps/battery-optimizer/files/service-overrides.yaml. This checks that every site-specific leaf is
# present with its exact value and type, and that no base config is vendored back into files/.

set -euo pipefail

FILES_DIR="apps/battery-optimizer/files"
OVERRIDES="$FILES_DIR/service-overrides.yaml"

if [[ ! -f "$OVERRIDES" ]]; then
    echo "ERROR: overrides file not found: $OVERRIDES"
    exit 1
fi

python3 - "$FILES_DIR" "$OVERRIDES" <<'PY'
import pathlib
import sys

import yaml

files_dir, overrides = sys.argv[1:3]
errors = []

for name in ("service.yaml", "tariff.yaml", "climate.yaml", "climate-mpc.yaml"):
    if (pathlib.Path(files_dir) / name).exists():
        errors.append(f"{files_dir}/{name} must not be vendored: the image ships it in /app/config")

with open(overrides) as f:
    cfg = yaml.safe_load(f)

# dotted path -> required value
expected = {
    "mode": "live",
    "planning.high_soc_cost_eur_per_kwh_h": 0.01,
    "planning.charge_soft_cap_eur_per_kwh": 0.005,
    "planning.discharge_margin_bound": True,
    "planning.thermal.mode": "shadow",
    "actuation.discharge_margin_w": 50,
    "actuation.load_following.enabled": True,
    "actuation.load_following.max_w": 2500,
    "full_charge.enabled": True,
    "climate_shadow.enabled": True,
    "climate_shadow.prior": "2311c010392f",
    "climate_shadow.optimizer_mode": "shadow",
}
for path, want in expected.items():
    node = cfg
    for key in path.split("."):
        node = node.get(key, None) if isinstance(node, dict) else None
    # type(): True == 1, but a bool must not match an int or the string "on"
    if node != want or type(node) is not type(want):
        errors.append(f"{path} is {node!r}, expected {want!r}")

for line in errors:
    print(f"ERROR: {line}")
if errors:
    print(f"\nFound {len(errors)} violation(s) in {overrides}; see docs/deployment/battery-optimizer.md")
    sys.exit(1)
print(f"OK: {overrides} holds all {len(expected)} site overrides")
PY
