#!/usr/bin/env bash
# Read-only, re-runnable health check of the Envoy Gateway ingress path (ingress-nginx -> Envoy
# Gateway migration, usable before and after any gateway or pfSense change).
#
# Checks, each printed as PASS, FAIL, WARN or INFO:
#   1  Envoy proxy Deployments ready for both gateways (internal, external)
#   2  Gateways Programmed and Accepted, listeners healthy (a listener with no attached route
#      only warns: passthrough and test listeners can have none by design)
#   3  every HTTPRoute, TLSRoute and TCPRoute attached to a gateway is Accepted
#   4  every SecurityPolicy is Accepted
#   5  per host (non-wildcard route hostnames), one unauthenticated GET on the gateway VIP via
#      curl --resolve: TLS verified, HTTP/2, status not 000 or 5xx
#   6  Forgejo SSH (TCP 29518) answers on the external VIP with a host key
#   7  argocd /api/webhook reaches argocd-server through the external VIP (400 = unsigned)
#   8  pfSense, read-only over ssh: alias k8s_external_ingress equals the external VIP, port
#      forward (rdr) rules follow the alias, host override count per IP, states to the old IP
#   9  DNS answers from the LAN resolver for a few names (external names -> external VIP,
#      an internal name -> internal VIP)
#  10  firing alerts (Watchdog and InfoInhibitor left out)
#  11  Argo CD applications that are not Synced and Healthy
#  12  Envoy access-log response flags UF, UH, URX, UC in the recent window
#
# FAIL means the path is broken or a gateway invariant is violated and sets the exit status to 1.
# WARN lists things to look at (alerts, Argo, response flags, listeners without routes) and does
# not change the exit status. Known quiet baselines are in the plan, not hard-coded here.
#
# --compare <old ip> adds a comparison against the previous controller (ingress-nginx-external,
# 192.168.193.4) while it still exists: per-host status differences (expected ones are listed in
# docs/conventions/gateway-api-routes.md), the SSH host key on both IPs, host overrides and pf
# states still pointing at the old IP. Drop it after Phase 6, when nginx is gone.
#
# Usage: scripts/gateway-check.sh [--context CTX] [--compare OLD_IP] [--compare-gateway GW]
#                                 [--pfsense HOST | --no-pfsense] [--dns IP]
#                                 [--alertmanager URL] [--since DURATION]
#   --context CTX          kubectl context (default grigri)
#   --compare OLD_IP       also probe OLD_IP and check pfSense for leftovers on it
#   --compare-gateway GW   gateway whose hosts are compared with OLD_IP (default external)
#   --pfsense HOST         ssh host with pfctl and unbound (default pfsense.grigri)
#   --no-pfsense           skip check 8
#   --dns IP               resolver for check 9 (default 192.168.192.1)
#   --alertmanager URL     Alertmanager base URL (default https://alertmanager.internal.grigri.cloud)
#   --since DURATION       log window for check 12 (default 30m)
# Needs kubectl, jq, curl; ssh, ssh-keyscan and dig for checks 6, 8 and 9 (skipped with a WARN
# when missing). No writes, no credentials, no sudo.
set -u

CONTEXT=grigri
COMPARE=
COMPARE_GW=external
PF_HOST=pfsense.grigri
USE_PF=1
DNS=192.168.192.1
ALERTMANAGER=https://alertmanager.internal.grigri.cloud
SINCE=30m
NS=envoy-gateway
GATEWAYS=(internal external)
MAX_TIME=12

usage() {
  sed -n '2,/^set -u$/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//' >&2
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --context) CONTEXT=${2:?--context needs a value}; shift ;;
    --compare) COMPARE=${2:?--compare needs an IP}; shift ;;
    --compare-gateway) COMPARE_GW=${2:?--compare-gateway needs a name}; shift ;;
    --pfsense) PF_HOST=${2:?--pfsense needs a host}; shift ;;
    --no-pfsense) USE_PF=0 ;;
    --dns) DNS=${2:?--dns needs an IP}; shift ;;
    --alertmanager) ALERTMANAGER=${2:?--alertmanager needs a URL}; shift ;;
    --since) SINCE=${2:?--since needs a duration}; shift ;;
    -h | --help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
  shift
done

if [[ -n $COMPARE && ! $COMPARE =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "--compare needs an IPv4 address, got: $COMPARE" >&2
  exit 2
fi
if [[ ! $SINCE =~ ^[0-9]+[smh]$ ]]; then
  echo "--since needs a duration like 30m, got: $SINCE" >&2
  exit 2
fi

for tool in kubectl jq curl; do
  command -v "$tool" > /dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

FAILS=0
WARNS=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }
warn() { printf 'WARN  %s\n' "$*"; WARNS=$((WARNS + 1)); }
info() { printf 'INFO  %s\n' "$*"; }

k() { kubectl --context "$CONTEXT" "$@"; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

declare -A VIP=()

# 1 + 2. proxies, gateways, listeners ---------------------------------------------------------
for gw in "${GATEWAYS[@]}"; do
  if ! k -n "$NS" get gateway "$gw" -o json > "$TMP/gw-$gw.json" 2> "$TMP/err"; then
    fail "gateway $gw: kubectl failed: $(head -n1 "$TMP/err")"
    continue
  fi
  VIP[$gw]=$(jq -r '.status.addresses[0].value // empty' "$TMP/gw-$gw.json")

  bad_deploys=$(k -n "$NS" get deploy -l "gateway.envoyproxy.io/owning-gateway-name=$gw" -o json 2> "$TMP/err" |
    jq -r '[.items[] | select((.status.readyReplicas // 0) != .spec.replicas or .spec.replicas == 0) | .metadata.name] | join(",")')
  n_deploys=$(k -n "$NS" get deploy -l "gateway.envoyproxy.io/owning-gateway-name=$gw" -o name 2> /dev/null | wc -l)
  if ((n_deploys == 0)); then
    fail "envoy $gw: no proxy Deployment found"
  elif [[ -n $bad_deploys ]]; then
    fail "envoy $gw: proxy Deployment not fully ready: $bad_deploys"
  else
    pass "envoy $gw: $n_deploys proxy Deployment(s) ready"
  fi

  notok=$(jq -r '[.status.conditions[]? | select((.type == "Programmed" or .type == "Accepted") and .status != "True") | .type] | join(",")' "$TMP/gw-$gw.json")
  if [[ -z ${VIP[$gw]} ]]; then
    fail "gateway $gw: no address in status"
  elif [[ -n $notok ]]; then
    fail "gateway $gw (${VIP[$gw]}): condition not True: $notok"
  else
    pass "gateway $gw programmed on ${VIP[$gw]}"
  fi

  jq -r '.status.listeners[]? | [.name, (.attachedRoutes // 0), ([.conditions[] | select(.status != "True") | .type] | join(","))] | @tsv' "$TMP/gw-$gw.json" > "$TMP/listeners-$gw"
  nl=0
  while IFS=$'\t' read -r name attached conds; do
    nl=$((nl + 1))
    if [[ -n $conds && $attached -gt 0 ]]; then
      fail "gateway $gw listener $name: condition not True ($conds), $attached route(s)"
    elif [[ -n $conds ]]; then
      warn "gateway $gw listener $name: condition not True ($conds), no route attached"
    fi
  done < "$TMP/listeners-$gw"
  bad_l=$(awk -F'\t' '$3 != "" && $2 > 0' "$TMP/listeners-$gw" | wc -l)
  ((bad_l == 0)) && pass "gateway $gw: $nl listeners, none unhealthy with routes"
  empty_l=$(awk -F'\t' '$2 == 0 {print $1}' "$TMP/listeners-$gw" | paste -sd, -)
  [[ -n $empty_l ]] && info "gateway $gw listeners without routes: $empty_l"
done

# 3. every route attached to a gateway is Accepted ----------------------------------------------
: > "$TMP/routes.json"
for kind in httproutes tlsroutes tcproutes; do
  if ! k get "$kind.gateway.networking.k8s.io" -A -o json >> "$TMP/routes.json" 2> "$TMP/err"; then
    warn "cannot list $kind: $(head -n1 "$TMP/err")"
  fi
done
for gw in "${GATEWAYS[@]}"; do
  bad=$(jq -s -r --arg gw "$gw" '
    [ .[].items[]
      | select([.spec.parentRefs[]?.name] | index($gw))
      | select(([.status.parents[]? | select(.parentRef.name == $gw) | .conditions[]? | select(.type == "Accepted" and .status == "True")] | length) == 0)
      | "\(.kind)/\(.metadata.namespace)/\(.metadata.name)" ] | join(" ")' "$TMP/routes.json")
  total=$(jq -s --arg gw "$gw" '[.[].items[] | select([.spec.parentRefs[]?.name] | index($gw))] | length' "$TMP/routes.json")
  if [[ -n $bad ]]; then
    fail "routes on $gw not Accepted: $bad"
  else
    pass "all $total routes on $gw Accepted"
  fi
done

# 4. SecurityPolicies --------------------------------------------------------------------------
if k get securitypolicy -A -o json > "$TMP/sp.json" 2> "$TMP/err"; then
  bad=$(jq -r '[.items[] | select(([.status.ancestors[]?.conditions[]? | select(.type == "Accepted" and .status == "True")] | length) == 0) | "\(.metadata.namespace)/\(.metadata.name)"] | join(" ")' "$TMP/sp.json")
  n=$(jq '.items | length' "$TMP/sp.json")
  if [[ -n $bad ]]; then
    fail "SecurityPolicies not Accepted: $bad"
  else
    pass "all $n SecurityPolicies Accepted"
  fi
else
  warn "cannot list SecurityPolicies: $(head -n1 "$TMP/err")"
fi

# 5. per-host probes on the VIPs ------------------------------------------------------------------
probe_hosts() { # gateway -> sorted unique non-wildcard route hostnames
  jq -s -r --arg gw "$1" '[.[].items[] | select(.kind == "HTTPRoute" and ([.spec.parentRefs[]?.name] | index($gw))) | .spec.hostnames[]?] | map(select(startswith("*") | not)) | unique[]' "$TMP/routes.json"
}
for gw in "${GATEWAYS[@]}"; do
  ip=${VIP[$gw]:-}
  [[ -n $ip ]] || continue
  probe_hosts "$gw" > "$TMP/hosts-$gw"
  n=0
  problems=0
  notes=0
  while read -r h; do
    [[ -n $h ]] || continue
    n=$((n + 1))
    read -r code verify httpv < <(curl -s -o /dev/null -m "$MAX_TIME" -w '%{http_code} %{ssl_verify_result} %{http_version}\n' --resolve "$h:443:$ip" "https://$h/" 2> /dev/null || echo '000 -1 0')
    if [[ $code == 000 || $code == 5?? || $verify != 0 || $httpv != 2 ]]; then
      fail "$gw $h on $ip: status=$code tls-verify=$verify http=$httpv"
      problems=$((problems + 1))
    elif [[ -n $COMPARE && $gw == "$COMPARE_GW" ]]; then
      old=$(curl -s -o /dev/null -m "$MAX_TIME" -w '%{http_code}' --resolve "$h:443:$COMPARE" "https://$h/" 2> /dev/null)
      if [[ $old != "$code" ]]; then
        info "$gw $h: $ip=$code $COMPARE=$old (expected differences: docs/conventions/gateway-api-routes.md)"
        notes=$((notes + 1))
      fi
    fi
  done < "$TMP/hosts-$gw"
  ((problems == 0)) && pass "$gw: $n hosts probed on $ip (TLS verified, h2, no 000/5xx)${COMPARE:+, $notes differ from $COMPARE}"
done

# 6. Forgejo SSH host key ----------------------------------------------------------------------------
hostkey() { timeout 10 ssh-keyscan -p 29518 "$1" 2> /dev/null | ssh-keygen -lf - 2> /dev/null | awk '{print $2}' | sort | paste -sd, -; }
if command -v ssh-keyscan > /dev/null && command -v ssh-keygen > /dev/null; then
  ext=${VIP[external]:-}
  if [[ -n $ext ]]; then
    hk=$(hostkey "$ext")
    if [[ -z $hk ]]; then
      fail "forgejo ssh 29518 on $ext: no host key"
    elif [[ -n $COMPARE ]]; then
      hko=$(hostkey "$COMPARE")
      if [[ $hk == "$hko" ]]; then
        pass "forgejo ssh 29518: same host key on $ext and $COMPARE ($hk)"
      else
        fail "forgejo ssh 29518: host key differs ($ext=$hk $COMPARE=${hko:-none})"
      fi
    else
      pass "forgejo ssh 29518 answers on $ext ($hk)"
    fi
  fi
else
  warn "ssh-keyscan or ssh-keygen missing: forgejo ssh check skipped"
fi

# 7. argocd webhook ---------------------------------------------------------------------------------------
if [[ -n ${VIP[external]:-} ]]; then
  w=$(curl -sk -o /dev/null -m 10 -X POST -w '%{http_code}' --resolve "argocd.grigri.cloud:443:${VIP[external]}" https://argocd.grigri.cloud/api/webhook 2> /dev/null)
  if [[ $w == 400 ]]; then
    pass "argocd /api/webhook reaches argocd-server on ${VIP[external]} (400 = unsigned, expected)"
  else
    fail "argocd /api/webhook on ${VIP[external]} = ${w:-none} (want 400)"
  fi
fi

# 8. pfSense, read-only ----------------------------------------------------------------------------------
if ((USE_PF)); then
  if command -v ssh > /dev/null; then
    remote='pfctl -t k8s_external_ingress -T show; echo @@RDR; pfctl -sn 2>/dev/null | grep -c "rdr.*k8s_external_ingress"; echo @@OVR; grep -E "^local-data: \"[^ ]+ A 192\.168\.193\." /var/unbound/host_entries.conf'
    if [[ -n $COMPARE ]]; then
      # states from this host are left out: the --compare probes open (and leave in FIN_WAIT_2)
      # states to the old IP themselves
      remote="$remote; echo @@STATES; sh -c 'me=\${SSH_CLIENT%% *}; pfctl -ss 2>/dev/null | grep \"$COMPARE:\" | grep -v \"\$me:\" | wc -l'"
    fi
    if ssh -o BatchMode=yes -o ConnectTimeout=8 "$PF_HOST" "$remote" > "$TMP/pf" 2> "$TMP/err"; then
      alias_val=$(sed -n '1,/^@@RDR$/p' "$TMP/pf" | grep -v '^@@' | tr -d ' ' | paste -sd, -)
      rdr=$(sed -n '/^@@RDR$/,/^@@OVR$/p' "$TMP/pf" | grep -v '^@@' | tr -d ' ')
      sed -n '/^@@OVR$/,/^@@STATES$/p' "$TMP/pf" | grep -v '^@@' | sed -E 's/.* A ([0-9.]+)".*/\1/' | sort | uniq -c | awk '{print $2 "=" $1}' | paste -sd' ' - > "$TMP/ovr"
      if [[ $alias_val == "${VIP[external]:-}" ]]; then
        pass "pfSense alias k8s_external_ingress = $alias_val (external VIP)"
      else
        fail "pfSense alias k8s_external_ingress = '${alias_val}' (external VIP is ${VIP[external]:-unknown})"
      fi
      if [[ ${rdr:-0} -ge 1 ]]; then pass "pfSense: $rdr rdr rules follow the alias"; else fail "pfSense: no rdr rule uses the alias"; fi
      info "pfSense host overrides per IP: $(cat "$TMP/ovr")"
      if [[ -n $COMPARE ]]; then
        o_old=$(tr ' ' '\n' < "$TMP/ovr" | awk -F= -v ip="$COMPARE" '$1 == ip {print $2}')
        st_old=$(sed -n '/^@@STATES$/,$p' "$TMP/pf" | grep -v '^@@' | tr -d ' ')
        if [[ ${o_old:-0} == 0 ]]; then
          pass "pfSense: 0 host overrides on $COMPARE"
        else
          fail "pfSense: ${o_old} host overrides still on $COMPARE (count them, never eyeball: Services > DNS Resolver > Host Overrides)"
        fi
        if [[ ${st_old:-0} == 0 ]]; then
          pass "pfSense: 0 states to $COMPARE (this host's probe states excluded)"
        else
          warn "pfSense: ${st_old} states still to $COMPARE (pf keeps the rdr target of an existing state: Diagnostics > States, filter the IP, Kill)"
        fi
      fi
    else
      warn "pfSense ($PF_HOST) not reachable over ssh: $(head -n1 "$TMP/err"); check 8 skipped"
    fi
  else
    warn "ssh missing: pfSense check skipped"
  fi
fi

# 9. DNS from the LAN resolver ------------------------------------------------------------------------------
if command -v dig > /dev/null; then
  check_dns() { # name want-ip
    local r
    r=$(dig +short +time=3 +tries=1 "$1" "@$DNS" 2> /dev/null | tail -n1)
    if [[ $r == "$2" ]]; then
      pass "dns $1 -> $r (via $DNS)"
    else
      fail "dns $1 -> '${r}' via $DNS (want $2)"
    fi
  }
  for h in idm.grigri.cloud git.grigri.cloud argocd.grigri.cloud app.staging.forkline.dev idm.staging.forkline.dev mintpsicologia.es www.mintpsicologia.es; do
    [[ -n ${VIP[external]:-} ]] && check_dns "$h" "${VIP[external]}"
  done
  [[ -n ${VIP[internal]:-} ]] && check_dns argocd.internal.grigri.cloud "${VIP[internal]}"
else
  warn "dig missing: DNS check skipped"
fi

# 10. firing alerts -----------------------------------------------------------------------------------------------
if curl -s -m 10 "$ALERTMANAGER/api/v2/alerts?active=true&silenced=false&inhibited=false" > "$TMP/alerts.json" 2> /dev/null && jq -e 'type == "array"' "$TMP/alerts.json" > /dev/null 2>&1; then
  n=$(jq '[.[] | select(.labels.alertname != "Watchdog" and .labels.alertname != "InfoInhibitor")] | length' "$TMP/alerts.json")
  if ((n == 0)); then
    pass "no alert firing (Watchdog and InfoInhibitor ignored)"
  else
    while read -r line; do
      warn "alert firing: $line"
    done < <(jq -r '.[] | select(.labels.alertname != "Watchdog" and .labels.alertname != "InfoInhibitor") | "\(.labels.alertname) \(.labels.severity // "-") \(.labels.namespace // "-")"' "$TMP/alerts.json" | sort -u)
  fi
else
  warn "Alertmanager $ALERTMANAGER not reachable or not JSON: alert check skipped"
fi

# 11. Argo CD applications ----------------------------------------------------------------------------------------
if k -n argocd get applications.argoproj.io -o json > "$TMP/apps.json" 2> "$TMP/err"; then
  jq -r '.items[] | select(.status.sync.status != "Synced" or .status.health.status != "Healthy") | "\(.metadata.name) sync=\(.status.sync.status) health=\(.status.health.status)"' "$TMP/apps.json" > "$TMP/apps-bad"
  if [[ -s $TMP/apps-bad ]]; then
    while read -r line; do warn "argo app $line"; done < "$TMP/apps-bad"
  else
    pass "all $(jq '.items | length' "$TMP/apps.json") Argo applications Synced and Healthy"
  fi
else
  warn "cannot list Argo applications: $(head -n1 "$TMP/err")"
fi

# 12. Envoy access-log response flags --------------------------------------------------------------------------------
: > "$TMP/flags"
for gw in "${GATEWAYS[@]}"; do
  for p in $(k -n "$NS" get pods -l "gateway.envoyproxy.io/owning-gateway-name=$gw" -o name 2> /dev/null); do
    k -n "$NS" logs "$p" -c envoy --since="$SINCE" 2> /dev/null |
      jq -R -r --arg gw "$gw" 'fromjson? | select(.response_flags != null and (.response_flags | test("UF|UH|URX|UC"))) | [$gw, .response_flags, .authority, .response_code] | @tsv' >> "$TMP/flags" 2> /dev/null
  done
done
if [[ -s $TMP/flags ]]; then
  while read -r cnt gw flag auth code; do
    warn "access log $gw: $cnt x flag $flag $auth status $code (last $SINCE)"
  done < <(sort "$TMP/flags" | uniq -c)
else
  pass "no UF/UH/URX/UC response flags in the last $SINCE (both gateways)"
fi

echo
if ((FAILS == 0)); then
  echo "RESULT: no FAIL ($WARNS warning(s))"
else
  echo "RESULT: $FAILS FAIL, $WARNS warning(s)"
fi
((FAILS == 0))
