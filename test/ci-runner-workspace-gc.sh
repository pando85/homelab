#!/usr/bin/env bash
# Tests for the workspace-gc script (platform/ci-runners/resources/workspace-gc-script.yaml).
# Extracts the shell script from the ConfigMap YAML, then runs scenarios with
# fake pgrep/df binaries to verify:
# - Static: no destructive commands (rm, stat) and no maintenance mode.
# - Online mode never deletes workspace dirs, even under extreme pressure.
# - Online mode exits when runner disappears.
# - Online mode survives pgrep/df failures without crashing or deleting.
#
# Usage: bash test/ci-runner-workspace-gc.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT_YAML="$REPO_ROOT/platform/ci-runners/resources/workspace-gc-script.yaml"

# --- Extract script from ConfigMap YAML ---
extract_script() {
  python3 - "$SCRIPT_YAML" <<'PY'
import sys, yaml
with open(sys.argv[1]) as f:
    cm = yaml.safe_load(f)
print(cm["data"]["cleanup.sh"], end="")
PY
}

# --- Test harness ---
PASS=0
FAIL=0

pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "FAIL: $1 — $2"; FAIL=$((FAIL + 1)); }

assert_contains() {
  if echo "$1" | grep -qF "$2"; then pass "$3"; else fail "$3" "output missing '$2'"; fi
}

assert_not_contains() {
  if echo "$1" | grep -qF "$2"; then fail "$3" "output unexpectedly contains '$2'"; else pass "$3"; fi
}

assert_dir_exists() {
  if [ -d "$1" ]; then pass "$2"; else fail "$2" "directory $1 does not exist"; fi
}

# --- Helper to run script with env vars ---
# Usage: run_script <timeout> <workspace> <fakebin> [extra_env...]
run_script() {
  local timeout="$1"
  local workspace="$2"
  local fakebin="$3"
  shift 3

  local script="$workspace/../cleanup.sh"
  extract_script > "$script"

  env "$@" \
    PATH="$fakebin:/usr/bin:/bin" \
    WORKSPACE_DIR="$workspace" \
    CHECK_INTERVAL="1" \
    THRESHOLD_MIN=30 \
    THRESHOLD_LOW=50 \
    THRESHOLD_MED=70 \
    THRESHOLD_HIGH=80 \
    THRESHOLD_CRITICAL=90 \
    THRESHOLD_EMERGENCY=95 \
    timeout "$timeout" sh "$script" 2>&1 || true
}

# --- Static analysis: no destructive commands ---
echo "=== Static: no destructive commands ==="
script_content=$(extract_script)

# Check for standalone 'rm' command (not in comments)
if echo "$script_content" | grep -v '^\s*#' | grep -qw 'rm'; then
  fail "Static: no rm command" "script contains 'rm' in non-comment lines"
else
  pass "Static: no rm command"
fi

# Check for stat -c (mtime check)
if echo "$script_content" | grep -q 'stat -c'; then
  fail "Static: no stat -c" "script contains 'stat -c'"
else
  pass "Static: no stat -c"
fi

# Check for maintenance mode env var
if echo "$script_content" | grep -q 'WORKSPACE_GC_MAINTENANCE'; then
  fail "Static: no maintenance mode" "script contains 'WORKSPACE_GC_MAINTENANCE'"
else
  pass "Static: no maintenance mode"
fi

# --- Scenario A: Online mode never deletes, even under extreme pressure ---
echo "=== Scenario A: online_no_delete ==="
tmpdir=$(mktemp -d /tmp/opencode/workspace-gc-test-XXXXXX)
workspace="$tmpdir/workspace"
fakebin="$tmpdir/bin"
mkdir -p "$workspace" "$fakebin"

# Create aged dirs: job workspace (16 hex), 2-hex cache bucket, preserved names, arbitrary
mkdir -p "$workspace/ec4edb78aea87248"
mkdir -p "$workspace/ac"
mkdir -p "$workspace/.pnpm-store"
mkdir -p "$workspace/tool_cache"
mkdir -p "$workspace/unrelated-dir"

# Age them all to 100000 seconds (~69 days)
touch -d "100000 seconds ago" "$workspace/ec4edb78aea87248"
touch -d "100000 seconds ago" "$workspace/ac"
touch -d "100000 seconds ago" "$workspace/.pnpm-store"
touch -d "100000 seconds ago" "$workspace/tool_cache"
touch -d "100000 seconds ago" "$workspace/unrelated-dir"

# Set up fake binaries
cat > "$fakebin/pgrep" <<'PGEOF'
#!/bin/sh
exit 0
PGEOF
chmod +x "$fakebin/pgrep"

cat > "$fakebin/df" <<'DFEOF'
#!/bin/sh
echo "Filesystem 1024-blocks Used Available Capacity Mounted"
echo "/dev/fake    100000       95000  5000  95%     /workspace"
DFEOF
chmod +x "$fakebin/df"

output=$(run_script 3 "$workspace" "$fakebin" ALERT_INTERVAL=60)

assert_contains "$output" "workspace disk usage: 95%" "A: logs usage"
assert_contains "$output" "ALERT disk usage 95%" "A: logs alert"
assert_not_contains "$output" "removing" "A: never removes"
assert_dir_exists "$workspace/ec4edb78aea87248" "A: job dir preserved"
assert_dir_exists "$workspace/ac" "A: 2-hex cache bucket preserved"
assert_dir_exists "$workspace/.pnpm-store" "A: .pnpm-store preserved"
assert_dir_exists "$workspace/tool_cache" "A: tool_cache preserved"
assert_dir_exists "$workspace/unrelated-dir" "A: arbitrary dir preserved"

rm -rf "$tmpdir"

# --- Scenario B: Alert throttling ---
echo "=== Scenario B: alert_throttling ==="
tmpdir=$(mktemp -d /tmp/opencode/workspace-gc-test-XXXXXX)
workspace="$tmpdir/workspace"
fakebin="$tmpdir/bin"
mkdir -p "$workspace" "$fakebin"

cat > "$fakebin/pgrep" <<'PGEOF'
#!/bin/sh
exit 0
PGEOF
chmod +x "$fakebin/pgrep"

cat > "$fakebin/df" <<'DFEOF'
#!/bin/sh
echo "Filesystem 1024-blocks Used Available Capacity Mounted"
echo "/dev/fake    100000       95000  5000  95%     /workspace"
DFEOF
chmod +x "$fakebin/df"

output=$(run_script 5 "$workspace" "$fakebin" ALERT_INTERVAL=2)

# With ALERT_INTERVAL=2 and 5s runtime, expect 2-3 alerts
alert_count=$(echo "$output" | grep -c "ALERT disk usage 95%" || echo 0)
if [ "$alert_count" -ge 2 ] && [ "$alert_count" -le 4 ]; then
  pass "B: alert throttled ($alert_count alerts in 5s)"
else
  fail "B: alert throttled" "expected 2-4 alerts, got $alert_count"
fi

rm -rf "$tmpdir"

# --- Scenario C: Online mode exits when runner disappears ---
echo "=== Scenario C: online_exit_on_runner_exit ==="
tmpdir=$(mktemp -d /tmp/opencode/workspace-gc-test-XXXXXX)
workspace="$tmpdir/workspace"
fakebin="$tmpdir/bin"
mkdir -p "$workspace" "$fakebin"

cat > "$fakebin/pgrep" <<'PGEOF'
#!/bin/sh
if [ -f "$PGREP_FLIP_FILE" ]; then exit 1; fi
touch "$PGREP_FLIP_FILE"
exit 0
PGEOF
chmod +x "$fakebin/pgrep"

cat > "$fakebin/df" <<'DFEOF'
#!/bin/sh
echo "Filesystem 1024-blocks Used Available Capacity Mounted"
echo "/dev/fake    100000       50000  50000  50%     /workspace"
DFEOF
chmod +x "$fakebin/df"

output=$(run_script 5 "$workspace" "$fakebin" ALERT_INTERVAL=60 "PGREP_FLIP_FILE=$tmpdir/pgrep-flip")

assert_contains "$output" "forgejo-runner exited, stopping workspace-gc" "C: exits on runner exit"

rm -rf "$tmpdir"

# --- Scenario D: pgrep error / df failure survival ---
echo "=== Scenario D: process_failure_survival ==="
tmpdir=$(mktemp -d /tmp/opencode/workspace-gc-test-XXXXXX)
workspace="$tmpdir/workspace"
fakebin="$tmpdir/bin"
mkdir -p "$workspace" "$fakebin"

# Create aged dirs
mkdir -p "$workspace/ec4edb78aea87248"
mkdir -p "$workspace/ac"
touch -d "100000 seconds ago" "$workspace/ec4edb78aea87248"
touch -d "100000 seconds ago" "$workspace/ac"

# pgrep returns error (rc=2)
cat > "$fakebin/pgrep" <<'PGEOF'
#!/bin/sh
exit 2
PGEOF
chmod +x "$fakebin/pgrep"

# df returns garbage
cat > "$fakebin/df" <<'DFEOF'
#!/bin/sh
echo "garbage output"
DFEOF
chmod +x "$fakebin/df"

output=$(run_script 3 "$workspace" "$fakebin" ALERT_INTERVAL=60)

# Script should not crash (timeout exits 124, not early exit)
# Directories must be preserved
assert_dir_exists "$workspace/ec4edb78aea87248" "D: job dir preserved on pgrep error"
assert_dir_exists "$workspace/ac" "D: cache bucket preserved on df failure"

rm -rf "$tmpdir"

# --- Summary ---
echo ""
echo "=== Summary ==="
echo "Passed: $PASS"
echo "Failed: $FAIL"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
