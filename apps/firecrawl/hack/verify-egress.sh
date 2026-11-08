#!/usr/bin/env bash
# Post-rollout verification of the Firecrawl egress isolation.
#
# Non-destructive and observation-only: health, one real public scrape, one search
# and one start-page-only crawl (the paths existing REST users depend on), then
# fetch attempts against private destinations, which must NOT succeed. Run it once
# ArgoCD has synced the NetworkPolicies, from anywhere that reaches the internal
# ingress (LAN or VPN).
#
#   bash apps/firecrawl/hack/verify-egress.sh
#   BASE_URL=https://firecrawl.internal.grigri.cloud bash apps/firecrawl/hack/verify-egress.sh
#
# Results are PASS / FAIL / INCONCLUSIVE, and only PASS+FAIL counts prove anything:
# a private target that is not reachable is only evidence of the policy working if
# the public baseline proved the same job path works. Cilium drops denied packets
# instead of refusing them, so a blocked target normally shows up as a failed job
# after the app's own timeout; if we never get an answer we can attribute, the
# probe is INCONCLUSIVE, never a silent pass.
#
# What this script does NOT prove (see README): same-pod loopback (NetworkPolicy
# does not police a container talking to its own 127.0.0.1), IPv6 (not allowed at
# all, so nothing to reach), depth beyond the start page, and the allowed internal
# paths (redis, postgres, SearXNG, DNS) - those are asserted statically by
# test/mcp-gateway/firecrawl-egress-test.py against the rendered chart.
#
# The instance runs with USE_DB_AUTHENTICATION=false, so no key is needed. If one
# ever is, export FIRECRAWL_API_KEY: it is only ever sent as a header, never
# printed. Response bodies are never printed either; for a denied probe only the
# job's own error field is echoed, sanitized to one short printable line, so a
# failure that is unrelated to egress cannot be mistaken for a denial.
set -euo pipefail

BASE_URL="${BASE_URL:-https://firecrawl.internal.grigri.cloud}"
SCRAPE_TIMEOUT_MS="${SCRAPE_TIMEOUT_MS:-15000}"    # the app's own budget for one job
CURL_MAX_TIME="${CURL_MAX_TIME:-120}"              # our bound on waiting for the answer

# Destinations a fetcher must never reach, all of them harmless health/metadata
# paths that return nothing personal and mutate nothing: the LAN gateway (a node
# address), the cloud-style metadata listing, the kube-dns service address (which
# lands on a pod inside 10.0.0.0/8 after DNAT), and internal hostnames that resolve
# to LAN addresses - the classic SSRF pivot, checked by the address it ends up at.
BLOCKED_TARGETS=(
  "http://192.168.192.1/"
  "http://169.254.169.254/latest/meta-data/"
  "http://10.43.0.10/"
  "http://idm.grigri.cloud/status"
  "http://firecrawl.internal.grigri.cloud/v0/health/readiness"
  "https://vault.internal.grigri.cloud/v1/sys/health"
)

# Signatures of "the fetch never completed". Anything else (a 4xx from the API, a
# queue or auth problem, a schema error) means the probe did not test egress.
DENIAL_RE='timed out|timeout|ECONNREFUSED|EHOSTUNREACH|ENETUNREACH|EAI_AGAIN|net::ERR|network error|connection refused|connection reset|could not connect|no route to host|failed to fetch|browser launch|status code 403|forbidden'

auth=()
if [ -n "${FIRECRAWL_API_KEY:-}" ]; then
  auth=(-H "Authorization: Bearer ${FIRECRAWL_API_KEY}")
fi

pass=0
fail=0
inconclusive=0

ok() { pass=$((pass + 1)); printf 'PASS         %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL         %s\n' "$1"; }
skip() { inconclusive=$((inconclusive + 1)); printf 'INCONCLUSIVE %s\n' "$1"; }

# call <path> <json> -> "<http status>|<success flag>|<sanitized error line>"
# The body goes to a temp file that is always removed; only the machine-readable
# status, the success flag and the job's own error text are returned.
call() {
  local file status success error
  file=$(mktemp)
  trap 'rm -f "$file"' RETURN
  status=$(curl -sS -o "$file" -w '%{http_code}' --max-time "$CURL_MAX_TIME" \
    "${auth[@]}" -H 'Content-Type: application/json' \
    -X POST "${BASE_URL}$1" -d "$2" || echo 000)
  if command -v jq >/dev/null 2>&1; then
    success=$(jq -r '.success // false' "$file" 2>/dev/null || echo false)
    error=$(jq -r '.error // .message // empty' "$file" 2>/dev/null || true)
  else
    success=$(grep -q '"success":true' "$file" && echo true || echo false)
    error=$(grep -o '"error":"[^"]*"' "$file" | head -1 | cut -d'"' -f4 || true)
  fi
  error=$(printf '%s' "$error" | tr -cd '[:print:] ' | tr -s ' ' | cut -c1-160)
  printf '%s|%s|%s' "$status" "$success" "$error"
}

echo "== health (read-only)"
health=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
  "${BASE_URL}/v0/health/readiness" || echo 000)
if [ "$health" = 200 ]; then
  ok "GET /v0/health/readiness -> 200"
else
  bad "GET /v0/health/readiness -> ${health} (expected 200)"
fi

echo "== public paths still work for existing REST users (baseline)"
scrape_payload="\"formats\":[\"markdown\"],\"timeout\":${SCRAPE_TIMEOUT_MS}"
for probe in \
  "/v2/scrape|{\"url\":\"https://example.com\",${scrape_payload}}|scrape example.com (browser + HTTP fetch engines)" \
  "/v2/search|{\"query\":\"firecrawl\",\"limit\":1,\"sources\":[\"web\"]}|search (SearXNG path)" \
  "/v2/crawl|{\"url\":\"https://example.com\",\"limit\":1,\"maxDepth\":1,\"scrapeOptions\":{${scrape_payload}}}|crawl one start page (worker queue + fetch engine)"; do
  path="${probe%%|*}"
  rest="${probe#*|}"
  payload="${rest%%|*}"
  label="${rest##*|}"
  result=$(call "$path" "$payload")
  if [ "$(printf '%s' "$result" | cut -d'|' -f1-2)" = "200|true" ]; then
    ok "POST ${path} ${label}"
  else
    bad "POST ${path} ${label} -> $(printf '%s' "$result" | cut -d'|' -f1,3) (expected 200|true)"
  fi
done

# The baseline decides what the private probes can prove. If the service cannot
# fetch a public page at all, a failure against a private address says nothing
# about the NetworkPolicies, so nothing is asserted and the run is inconclusive.
if [ "$fail" -gt 0 ]; then
  echo
  echo "INCONCLUSIVE verify-egress: the public baseline did not pass, so the"
  echo "             private-destination probes below cannot prove egress denial."
  echo "             Fix the baseline (or the rollout) and run this again."
  exit 1
fi

echo "== private destinations are denied"
for target in "${BLOCKED_TARGETS[@]}"; do
  started=$SECONDS
  result=$(call "/v2/scrape" "{\"url\":\"${target}\",${scrape_payload}}")
  took=$((SECONDS - started))
  status=$(printf '%s' "$result" | cut -d'|' -f1)
  success=$(printf '%s' "$result" | cut -d'|' -f2)
  error=$(printf '%s' "$result" | cut -d'|' -f3-)
  if [ "$success" = "true" ]; then
    bad "scrape ${target} -> reached it (status ${status})"
  elif [ "$status" = "000" ] || [ "$took" -ge "$CURL_MAX_TIME" ]; then
    skip "scrape ${target} -> no answer within ${CURL_MAX_TIME}s, cannot attribute"
  elif printf '%s' "$error" | grep -qiE "$DENIAL_RE"; then
    ok "scrape ${target} denied in ${took}s (${status}: ${error})"
  else
    skip "scrape ${target} -> ${status} without an egress-failure signal (${error})"
  fi
done

echo
if [ "$fail" -gt 0 ]; then
  echo "FAIL verify-egress: ${fail} of $((pass + fail)) assertions failed, ${inconclusive} inconclusive, against ${BASE_URL}"
  exit 1
fi
if [ "$inconclusive" -gt 0 ]; then
  echo "INCONCLUSIVE verify-egress: ${pass} proven, ${inconclusive} unproven against ${BASE_URL}"
  echo "             Treat the isolation as unverified until those are resolved."
  exit 1
fi
if [ "$pass" -lt 4 ]; then
  echo "INCONCLUSIVE verify-egress: only ${pass} checks ran against ${BASE_URL}"
  exit 1
fi
echo "PASS verify-egress: ${pass} checks against ${BASE_URL}"
