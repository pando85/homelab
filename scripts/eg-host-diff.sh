#!/usr/bin/env bash
# Read-only diff of every Ingress host/path served by ingress-nginx against the same
# request served by Envoy Gateway (ingress-nginx -> Envoy Gateway migration, Gate 3).
#
# For each host and path it sends one unauthenticated GET to the nginx VIP and to the Envoy
# Gateway VIP (curl --resolve, so DNS is not involved) and prints a TSV:
#   namespace  host  path  nginx  envoy  verdict
# where nginx/envoy are "<status> <redirect-url>" (redirect normalised, see normalise_redirect).
#
# Verdicts:
#   SAME          identical status and (normalised) redirect
#   SAME-AUTH     both sides redirect into the auth flow (nginx: oauth2-proxy /oauth2/start,
#                 Envoy: Kanidm /ui/oauth2); the URLs differ by design
#   DIFF          anything else
#   ENVOY-MISSING Envoy has no route for the request (404 without content-length/content-type)
#                 although the Ingress declares it (an undeclared "/" that nginx also 404s
#                 counts as SAME)
#   ENVOY-OK      (--envoy-only) Envoy answered something other than a no-route 404
#
# Usage: scripts/eg-host-diff.sh [--envoy-only] [--context CTX] [--delay SECS]
# Output goes to stdout, progress and the summary to stderr. No credentials are sent.
# For the overall health of the path (gateways, routes, pfSense, DNS, alerts) see scripts/gateway-check.sh.
set -euo pipefail

CONTEXT=grigri
DELAY=0.2
ENVOY_ONLY=0
MAX_TIME=15

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//' >&2
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --envoy-only) ENVOY_ONLY=1 ;;
    --context) CONTEXT=${2:?--context needs a value}; shift ;;
    --delay) DELAY=${2:?--delay needs a value}; shift ;;
    -h | --help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
  shift
done

for tool in kubectl jq curl; do
  command -v "$tool" > /dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

# ingress class -> "<nginx IP> <Envoy IP>"
declare -A NGINX_IP=([nginx-internal]=192.168.193.1 [nginx-external]=192.168.193.4)
declare -A ENVOY_IP=([nginx-internal]=192.168.193.13 [nginx-external]=192.168.193.14)

# Redirects that differ by design are collapsed to AUTH-REDIRECT: nginx sends the browser to
# oauth2-proxy (<host>/oauth2/start?...), Envoy OIDC sends it straight to the IdP (Kanidm:
# https://idm.grigri.cloud/ui/oauth2?..., Keycloak: .../protocol/openid-connect/auth?...).
# Everything else is compared literally, except that an explicit :443 (nginx X-Forwarded-Port)
# is dropped and a random per-request rid= (FreshRSS) is masked.
normalise_redirect() {
  local url=$1
  case $url in
    '') ;;
    *://*/oauth2/start* | https://idm.grigri.cloud/ui/oauth2* | */protocol/openid-connect/auth\?*)
      url=AUTH-REDIRECT ;;
    *) url=$(sed -E 's#^(https?://[^/:]+):443#\1#; s#([?&]rid=)[0-9a-f]+#\1RANDOM#' <<< "$url") ;;
  esac
  printf '%s' "$url"
}

HDRS=$(mktemp)
trap 'rm -f "$HDRS"' EXIT

# probe <host> <ip> <path> -> "<status>|<redirect>|<noroute>"
# noroute=1 marks an Envoy "no route" answer: a 404 with neither content-length nor content-type
# (an upstream 404 always carries one of them, even when empty).
probe() {
  local host=$1 ip=$2 path=$3 out code redir noroute=0
  : > "$HDRS"
  out=$(curl -sk -o /dev/null --max-time "$MAX_TIME" -D "$HDRS" \
    --resolve "${host}:443:${ip}" \
    -w '%{http_code}|%{redirect_url}' \
    "https://${host}${path}" 2> /dev/null) || true
  [[ -n $out ]] || out='000|'
  code=${out%%|*}
  redir=${out#*|}
  if [[ $code == 404 ]] && ! grep -qiE '^(content-length|content-type):' "$HDRS"; then
    noroute=1
  fi
  printf '%s|%s|%s' "$code" "$redir" "$noroute"
}

# A sample request path for an Ingress path: regex capture groups are cut and replaced by a
# trailing slash, an empty path becomes /.
sample_path() {
  local p=$1
  if [[ -z $p ]]; then
    echo /
  elif [[ $p == *'('* ]]; then
    p=${p%%(*}
    [[ $p == */ ]] || p+=/
    echo "$p"
  else
    echo "$p"
  fi
}

# One row per class, host, path across all Ingresses (namespaces joined for shared hosts).
mapfile -t ROWS < <(
  kubectl --context="$CONTEXT" get ingress -A -o json | jq -r '
    [ .items[] | .metadata.namespace as $ns | (.spec.ingressClassName // "none") as $c
      | .spec.rules[]? | select(.host != null) | .host as $h
      | ((.http.paths // [{path: "/"}])[] | [$c, $h, ((.path // "") | if . == "" then "/" else . end), $ns, 1], [$c, $h, "/", $ns, 0]) ]
    | group_by(.[0:3]) | map([.[0][0], .[0][1], .[0][2], ([.[][3]] | unique | join(",")), ([.[][4]] | max)])
    | sort_by(.[1], .[2]) | .[] | @tsv'
)

printf 'namespace\thost\tpath\tnginx\tenvoy\tverdict\n'

declare -A COUNT=()
declare -A HTTP_HOST=()
total=0
for row in "${ROWS[@]}"; do
  IFS=$'\t' read -r class host raw_path ns declared <<< "$row"
  if [[ -z ${NGINX_IP[$class]:-} ]]; then
    echo "skip ${host}: unknown ingress class '${class}'" >&2
    continue
  fi
  path=$(sample_path "$raw_path")
  [[ -z ${HTTP_HOST[$class]:-} ]] && HTTP_HOST[$class]=$host

  IFS='|' read -r ecode eredir enoroute <<< "$(probe "$host" "${ENVOY_IP[$class]}" "$path")"
  envoy="${ecode} $(normalise_redirect "$eredir")"
  envoy_raw="${ecode} ${eredir}"

  if ((ENVOY_ONLY)); then
    nginx=-
    if ((enoroute && declared)); then verdict=ENVOY-MISSING; else verdict=ENVOY-OK; fi
  else
    IFS='|' read -r ncode nredir _ <<< "$(probe "$host" "${NGINX_IP[$class]}" "$path")"
    nginx="${ncode} $(normalise_redirect "$nredir")"
    if ((enoroute)) && ! ((declared == 0 && ncode == 404)); then
      # no route on Envoy; a 404 on nginx for a path the Ingress does not declare is not a gap
      verdict=ENVOY-MISSING
    elif [[ $nginx == "$envoy" ]]; then
      if [[ $envoy == *AUTH-REDIRECT* ]]; then verdict=SAME-AUTH; else verdict=SAME; fi
    else
      verdict=DIFF
    fi
    # keep the real URLs visible for DIFF rows
    if [[ $verdict == DIFF ]]; then
      nginx="${ncode} ${nredir}"
      envoy=$envoy_raw
    fi
  fi

  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$ns" "$host" "$path" "$nginx" "$envoy" "$verdict"
  COUNT[$verdict]=$((${COUNT[$verdict]:-0} + 1))
  total=$((total + 1))
  sleep "$DELAY"
done

# Port 80 on one host per gateway (known: Envoy 301, nginx 308).
for class in nginx-internal nginx-external; do
  host=${HTTP_HOST[$class]:-}
  [[ -n $host ]] || continue
  res=()
  for ip in "${NGINX_IP[$class]}" "${ENVOY_IP[$class]}"; do
    if ((ENVOY_ONLY)) && [[ $ip == "${NGINX_IP[$class]}" ]]; then
      res+=(-)
      continue
    fi
    out=$(curl -s -o /dev/null --max-time "$MAX_TIME" --resolve "${host}:80:${ip}" \
      -w '%{http_code} %{redirect_url}' "http://${host}/" 2> /dev/null) || out='000 '
    res+=("$out")
  done
  printf 'port80\t%s\t/\t%s\t%s\t%s\n' "$host" "${res[0]}" "${res[1]}" \
    "$([[ ${res[0]%% *} == "${res[1]%% *}" ]] && echo SAME || echo 'DIFF (expected: nginx 308, Envoy 301)')"
done

{
  echo "hosts/paths probed: ${total}"
  for v in "${!COUNT[@]}"; do echo "  ${v}: ${COUNT[$v]}"; done | sort
} >&2
