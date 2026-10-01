#!/usr/bin/env bash
# Regenerate apps/firecrawl/files/nuq.sql from upstream firecrawl release.
#
# Usage:
#   apps/firecrawl/hack/update-nuq-sql.sh [VERSION]
#
# VERSION defaults to the firecrawl image tag in values.yaml (no 'v' prefix).
# Upstream tag is v$VERSION.
#
# Transforms applied to upstream SQL:
#   - Remove all SQL comment lines (lines starting with optional whitespace + --)
#   - Remove ALTER SYSTEM SET lines (managed via Zalando CR parameters)
#   - Remove SELECT pg_reload_conf() (not applicable in init-container context)
#   - Collapse consecutive blank lines to single blank lines
#   - Prepend provenance header (source URL, git ref)
#   - Wrap in pg_advisory_lock/unlock to serialize concurrent init-container runs
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHART_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VALUES_FILE="$CHART_DIR/values.yaml"
OUTPUT_FILE="$CHART_DIR/files/nuq.sql"

TAG="${1:-}"
if [ -z "$TAG" ]; then
    TAG=$(sed -n '/depName=ghcr.io\/firecrawl\/firecrawl/,/tag:/{s/.*tag: *//p;}' "$VALUES_FILE" \
        | head -1 | tr -d ' "'"'")
    if [ -z "$TAG" ]; then
        echo "ERROR: cannot parse firecrawl image tag from $VALUES_FILE" >&2
        exit 1
    fi
fi

UPSTREAM_URL="https://raw.githubusercontent.com/firecrawl/firecrawl/v${TAG}/apps/nuq-postgres/nuq.sql"

TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT

if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$UPSTREAM_URL" -o "$TMPFILE"
elif command -v wget >/dev/null 2>&1; then
    wget -qO "$TMPFILE" "$UPSTREAM_URL"
elif command -v python3 >/dev/null 2>&1; then
    python3 -c "
import urllib.request, sys
try:
    urllib.request.urlretrieve(sys.argv[1], sys.argv[2])
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
" "$UPSTREAM_URL" "$TMPFILE"
else
    echo "ERROR: need curl, wget, or python3 to download upstream SQL" >&2
    exit 1
fi

{
    printf '%s\n' "-- Source: $UPSTREAM_URL"
    printf '%s\n' "-- Git ref: v${TAG}"
    printf '%s\n' "-- Adapted: for idempotent re-runs via init container"
    printf '%s\n' "-- Changes: removed ALTER SYSTEM statements (set via Zalando CR instead),"
    printf '%s\n' "--          removed SELECT pg_reload_conf(), kept pg_cron jobs (pg_cron available),"
    printf '%s\n' "--          CREATE TYPE already guarded upstream with DO \$\$ / EXCEPTION blocks."
    printf '\n'
    printf '%s\n' "-- Serialize concurrent init-container runs (api/worker/nuq-worker roll together)"
    printf '%s\n' "SELECT pg_advisory_lock(74920193);"
    printf '\n'

    sed \
        -e 's/[[:space:]]*--.*$//' \
        -e '/^[[:space:]]*ALTER SYSTEM/d' \
        -e '/pg_reload_conf/d' \
        "$TMPFILE" \
    | awk 'NF{print;p=1;next} !NF{if(p){print;p=0}}' \
    | awk '
        /^CREATE INDEX / {
            if (has_blank && prev_line ~ /^CREATE INDEX /) {
                has_blank = 0
            }
            if (has_blank) {
                print ""
                has_blank = 0
            }
            prev_line = $0
            print
            next
        }
        /^$/ {
            has_blank = 1
            next
        }
        {
            if (has_blank) {
                print ""
                has_blank = 0
            }
            prev_line = $0
            print
        }
    '

    printf '\n'
    printf '%s\n' "SELECT pg_advisory_unlock(74920193);"
} > "$OUTPUT_FILE"

echo "Generated $OUTPUT_FILE from upstream v${TAG}"
