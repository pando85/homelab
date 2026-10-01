#!/usr/bin/env bash
# Pre-commit guard: verify files/nuq.sql Git ref matches the firecrawl image tag in values.yaml.
# No network access required.
#
# Usage:
#   apps/firecrawl/hack/check-nuq-version.sh
#
# Override paths via environment for testing:
#   NUQ_SQL=path/to/nuq.sql VALUES_YAML=path/to/values.yaml \
#     apps/firecrawl/hack/check-nuq-version.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHART_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

NUQ_SQL="${NUQ_SQL:-$CHART_DIR/files/nuq.sql}"
VALUES_YAML="${VALUES_YAML:-$CHART_DIR/values.yaml}"

sql_ref=$(sed -n 's/^-- Git ref: v//p' "$NUQ_SQL" | head -1)
if [ -z "$sql_ref" ]; then
    echo "ERROR: cannot find '-- Git ref: v...' in $NUQ_SQL" >&2
    exit 1
fi

image_tag=$(sed -n '/depName=ghcr.io\/firecrawl\/firecrawl/,/tag:/{s/.*tag: *//p;}' "$VALUES_YAML" \
    | head -1 | tr -d ' "'"'")
if [ -z "$image_tag" ]; then
    echo "ERROR: cannot parse firecrawl image tag from $VALUES_YAML" >&2
    exit 1
fi

if [ "$sql_ref" != "$image_tag" ]; then
    echo "ERROR: nuq.sql is for v${sql_ref} but firecrawl image tag is ${image_tag}." >&2
    echo "Run: apps/firecrawl/hack/update-nuq-sql.sh ${image_tag}" >&2
    exit 1
fi
