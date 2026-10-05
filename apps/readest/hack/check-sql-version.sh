#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHART_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

SQL_DIR="${SQL_DIR:-$CHART_DIR/files/sql}"
VALUES_YAML="${VALUES_YAML:-$CHART_DIR/values.yaml}"

VERSION_FILE="$SQL_DIR/VERSION"
if [ ! -f "$VERSION_FILE" ]; then
    echo "ERROR: $VERSION_FILE not found. Run apps/readest/hack/vendor-sql-migrations.py" >&2
    exit 1
fi

vendored_version=$(tr -d '[:space:]' < "$VERSION_FILE")
if [ -z "$vendored_version" ]; then
    echo "ERROR: $VERSION_FILE is empty" >&2
    exit 1
fi

image_tag=$(sed -n '/depName=ghcr\.io\/readest\/readest/,/tag:/{s/.*tag: *//p;}' "$VALUES_YAML" \
    | head -1 | tr -d ' "'"'")
if [ -z "$image_tag" ]; then
    echo "ERROR: cannot parse readest image tag from $VALUES_YAML" >&2
    exit 1
fi

if [ "$vendored_version" != "$image_tag" ]; then
    echo "ERROR: vendored SQL is for v${vendored_version} but readest image tag is ${image_tag}." >&2
    echo "Run: apps/readest/hack/vendor-sql-migrations.py ${image_tag}" >&2
    exit 1
fi

readest_tags=$(sed -n '/depName=ghcr\.io\/readest\/readest/,/tag:/{s/.*tag: *//p;}' "$VALUES_YAML" \
    | tr -d ' "'"'")
unique_tags=$(echo "$readest_tags" | sort -u | wc -l)
if [ "$unique_tags" -ne 1 ]; then
    echo "ERROR: readest image tags are not consistent:" >&2
    echo "$readest_tags" >&2
    echo "All ghcr.io/readest/readest tags must be identical." >&2
    exit 1
fi

sql_count=0
for f in "$SQL_DIR"/[0-9]*_*.sql; do
    [ -f "$f" ] && sql_count=$((sql_count + 1))
done
if [ "$sql_count" -eq 0 ]; then
    echo "ERROR: no migration SQL files found in $SQL_DIR" >&2
    exit 1
fi

if [ ! -f "$SQL_DIR/schema.sql" ]; then
    echo "ERROR: schema.sql not found in $SQL_DIR" >&2
    exit 1
fi

if [ ! -s "$SQL_DIR/schema.sql" ]; then
    echo "ERROR: schema.sql is empty" >&2
    exit 1
fi

echo "OK: vendored SQL v${vendored_version} matches image tag, $sql_count migrations present"
