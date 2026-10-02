#!/usr/bin/env bash
# Audit and reclaim orphaned OpenEBS ZFS LocalPV volumes.
#
# DRY RUN IS THE DEFAULT. Nothing is modified unless you pass --apply.
#
#   scripts/zfs-orphan-audit.sh                            # plan only
#   scripts/zfs-orphan-audit.sh --include-snapshotted      # also plan snapshot-holders
#   scripts/zfs-orphan-audit.sh --apply                    # execute the plan
#   scripts/zfs-orphan-audit.sh --apply --include-snapshotted
#
# WHY THIS EXISTS
#   openebs-zfspv and fast-zfspv use `reclaimPolicy: Retain`, so deleting a PV
#   never triggers a CSI DeleteVolume call and the backing ZFS dataset is never
#   destroyed. The ZFSVolume CR survives too - it carries no ownerReference, so
#   nothing garbage-collects it. A ZFSVolume CR with no matching PV is therefore
#   the exact signature of a leak, and it is detectable entirely in-cluster:
#   the CR is named identically to its PV (`pvc-<uuid>` for dynamic provisioning,
#   the PV name for static provisioning).
#
#   Retain is deliberate, not an oversight: it is the only backstop against an
#   ArgoCD prune destroying a database, since only ~40% of PVCs carry
#   `Prune=false`. The fix is detection, not flipping the policy to Delete.
#   Before this script existed, 89 volumes / 139.6 GiB accumulated unnoticed
#   over 9 months. See docs/troubleshooting/cluster-hygiene.md.
#
#   The ZFSVolumeOrphaned PrometheusRule in
#   system/monitoring/resources/storage-prometheus-rules.yaml alerts on the same
#   condition. This script is the runbook it points at.
#
# SAFETY
#   Every candidate is re-verified live at run time and downgraded to SKIP if it
#   has acquired any of: a matching PV, a PVC referencing it as its volumeName, a
#   ZFSBackup / ZFSSnapshot / ZFSRestore CR, an active mount, or on-disk
#   snapshots. Snapshot-holders are a SOFT blocker (they are usually stale
#   artifacts of a dead volume) and need --include-snapshotted; everything else
#   is a HARD blocker and is never overridden, even under --apply.
#
# REQUIREMENTS
#   kubectl context `grigri`; passwordless ssh to each ZFS node.
set -uo pipefail

CTX="${ZFS_AUDIT_CONTEXT:-grigri}"
# shellcheck disable=SC2016  # expanded on the remote node, not here
ZP='export PATH=$PATH:/usr/sbin:/sbin'
MODE=dry
INCLUDE_SNAPPED=0

for a in "$@"; do
  case "$a" in
    --apply)               MODE=apply ;;
    --dry-run)             MODE=dry ;;
    --include-snapshotted) INCLUDE_SNAPPED=1 ;;
    -h|--help)             sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "usage: $0 [--dry-run|--apply] [--include-snapshotted]" >&2; exit 2 ;;
  esac
done

echo "Gathering live cluster + ZFS state..."

# --------------------------------------------------------------- cluster side
PV_NAMES=$(kubectl --context="$CTX" get pv -o name 2>/dev/null | sed 's|.*/||' | sort -u)
PVC_VOLS=$(kubectl --context="$CTX" get pvc -A \
  -o jsonpath='{range .items[*]}{.spec.volumeName}{"\n"}{end}' 2>/dev/null | sort -u)
BK=$(kubectl --context="$CTX" get zfsbackups.zfs.openebs.io -A -o name 2>/dev/null \
  | sed 's|.*/||;s|\..*||' | sort -u)
SN=$(kubectl --context="$CTX" get zfssnapshots.zfs.openebs.io -A \
  -o jsonpath='{range .items[*]}{.spec.volName}{"\n"}{end}' 2>/dev/null | sort -u)
RS=$(kubectl --context="$CTX" get zfsrestores.zfs.openebs.io -A \
  -o jsonpath='{range .items[*]}{.spec.volName}{"\n"}{end}' 2>/dev/null | sort -u)

# ZFSVolume CRs: name, namespace, pool, owner node. One line per CR.
CRS=$(kubectl --context="$CTX" get zfsvolumes.zfs.openebs.io -A \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.namespace}{"\t"}{.spec.poolName}{"\t"}{.spec.ownerNodeID}{"\t"}{.metadata.deletionTimestamp}{"\n"}{end}' 2>/dev/null)
[ -z "$CRS" ] && { echo "no ZFSVolume CRs found - is the openebs zfs-localpv operator installed?" >&2; exit 1; }

has() { [ -n "${2:-}" ] && printf '%s\n' "$1" | grep -qxF -- "$2"; }

# ------------------------------------------------------------------ host side
# One ssh per node per pool, not per volume.
declare -A DISK_USED DISK_MNT DISK_SNAP HOST_OK
NODES=$(printf '%s\n' "$CRS" | awk -F'\t' '$4!=""{print $4}' | sort -u)
POOLS=$(printf '%s\n' "$CRS" | awk -F'\t' '$4!="" && $3!="" {print $4"\t"$3}' | sort -u)

while IFS=$'\t' read -r node pool; do
  [ -z "$node" ] && continue
  [ "${HOST_OK[$node]:-unset}" = "unset" ] && {
    if ssh -n -o BatchMode=yes -o ConnectTimeout=8 "$node" "$ZP; zpool list -H -o name" >/dev/null 2>&1; then
      HOST_OK[$node]=yes
    else
      HOST_OK[$node]=no
    fi
  }
  [ "${HOST_OK[$node]}" = no ] && continue
  while IFS=$'\t' read -r n u m; do
    [ -z "${n:-}" ] && continue
    DISK_USED[$node:$n]=$u; DISK_MNT[$node:$n]=$m
  done < <(ssh -n -o BatchMode=yes "$node" "$ZP; zfs list -H -p -o name,used,mounted -r '$pool'" 2>/dev/null)
  while IFS= read -r n; do
    [ -z "$n" ] && continue
    p=${n%%@*}
    DISK_SNAP[$node:$p]=$(( ${DISK_SNAP[$node:$p]:-0} + 1 ))
  done < <(ssh -n -o BatchMode=yes "$node" "$ZP; zfs list -H -o name -t snapshot -r '$pool'" 2>/dev/null)
done <<< "$POOLS"

gib() { awk -v b="${1:-0}" 'BEGIN{printf "%.2f", b/1073741824}'; }

# ------------------------------------------------------------------- planning
TOTAL_BYTES=0; N_DEL=0; N_DEL_R=0; N_CRONLY=0; N_SKIP=0; N_UNREACH=0
PLAN=(); PLAN_ACT=(); PLAN_NODE=(); PLAN_DS=(); PLAN_R=(); PLAN_NS=()

while IFS=$'\t' read -r vol ns pool node term; do
  [ -z "$vol" ] && continue
  # already mid-deletion from an earlier run: surface it, don't re-plan silently
  [ -n "${term:-}" ] && TERM_NOTE="ALREADY-TERMINATING(since ${term}) " || TERM_NOTE=""
  [ -z "$node" ] && node="<unset>"
  [ -z "$pool" ] && pool="<unset>"
  # not an orphan if a PV of the same name exists
  has "$PV_NAMES" "$vol" && continue

  ds="$pool/$vol"
  act=DELETE; why=""; soft=""

  # hard blockers
  has "$PVC_VOLS" "$vol" && why="${why}referenced-by-PVC "
  has "$BK" "$vol"       && why="${why}has-ZFSBackup "
  has "$SN" "$vol"       && why="${why}has-ZFSSnapshot-CR "
  has "$RS" "$vol"       && why="${why}has-ZFSRestore "
  [ "${DISK_MNT[$node:$ds]:-no}" = yes ] && why="${why}MOUNTED "
  # soft blocker: stale on-disk snapshots with no CR
  [ "${DISK_SNAP[$node:$ds]:-0}" -gt 0 ] && soft="has-${DISK_SNAP[$node:$ds]}-stale-on-disk-snapshot(s) "

  bytes=${DISK_USED[$node:$ds]:-}
  ondisk=no; [ -n "$bytes" ] && ondisk=yes

  if [ "${HOST_OK[$node]:-no}" = no ]; then
    act=CR-ONLY-no-zfs-on-node; N_UNREACH=$((N_UNREACH+1)); why="node $node has no ZFS/unreachable "
  elif [ -n "$why" ]; then
    act=SKIP; N_SKIP=$((N_SKIP+1))
  elif [ -n "$soft" ] && [ "$INCLUDE_SNAPPED" -eq 1 ]; then
    act=DELETE-R; why="$soft"; N_DEL=$((N_DEL+1)); N_DEL_R=$((N_DEL_R+1)); TOTAL_BYTES=$((TOTAL_BYTES+bytes))
  elif [ -n "$soft" ]; then
    act=SKIP-needs-flag; why="$soft"; N_SKIP=$((N_SKIP+1))
  elif [ "$ondisk" = no ]; then
    act=CR-ONLY-no-dataset; N_CRONLY=$((N_CRONLY+1))
  else
    N_DEL=$((N_DEL+1)); TOTAL_BYTES=$((TOTAL_BYTES+bytes))
  fi

  printf -v line '  %-22s %-12s %9s GiB  %-46s %-22s %s%s' \
    "$act" "$node" "$(gib "${bytes:-0}")" "$vol" "$ds" "$TERM_NOTE" "$why"
  PLAN+=("$line"); PLAN_ACT+=("$act"); PLAN_NODE+=("$node"); PLAN_DS+=("$ds")
  PLAN_R+=("$([ "${act}" = DELETE-R ] && echo 1 || echo 0)"); PLAN_NS+=("${ns:-zfs-localpv}")
done <<< "$CRS"

echo
echo "=============================== ACTION PLAN ==============================="
[ ${#PLAN[@]} -eq 0 ] && echo "  no orphaned ZFSVolume CRs found - nothing to do."
printf '%s\n' "${PLAN[@]:-}"
echo
echo "  would destroy : $N_DEL datasets, $(gib $TOTAL_BYTES) GiB  ($N_DEL_R need 'zfs destroy -r')"
echo "  would CR-only : $N_CRONLY (no dataset on disk) + $N_UNREACH (node has no ZFS)"
echo "  would SKIP    : $N_SKIP"
echo "==========================================================================="

if [ "$N_SKIP" -gt 0 ]; then
  echo
  echo "NOTE: $N_SKIP entries were SKIPPED. 'SKIP-needs-flag' means the only blocker is"
  echo "      stale on-disk snapshots with no ZFSSnapshot/ZFSBackup CR; add"
  echo "      --include-snapshotted to destroy them with 'zfs destroy -r'."
  echo "      Any other SKIP reason is a HARD blocker - do not override it."
fi

if [ "$MODE" != apply ]; then
  echo
  echo "DRY RUN - nothing was modified. Re-run with --apply to execute."
  exit 0
fi

# -------------------------------------------------------------------- execute
echo
echo "APPLYING."
FAIL=0
STUCK=()

ds_exists() { ssh -n -o BatchMode=yes "$1" "$ZP; zfs list -H -o name '$2' >/dev/null 2>&1"; }
cr_exists() { kubectl --context="$CTX" -n "$1" get zfsvolume "$2" >/dev/null 2>&1; }

for i in "${!PLAN_ACT[@]}"; do
  act=${PLAN_ACT[$i]}; node=${PLAN_NODE[$i]}; ds=${PLAN_DS[$i]}; ns=${PLAN_NS[$i]}
  vol=${ds##*/}
  rflag=""; [ "${PLAN_R[$i]}" = 1 ] && rflag="-r "
  case "$act" in DELETE|DELETE-R|CR-ONLY*) ;; *) continue ;; esac

  # CR-only: nothing on disk, so just drop the CR.
  case "$act" in
    CR-ONLY-no-dataset)
      kubectl --context="$CTX" -n "$ns" delete zfsvolume "$vol" --ignore-not-found --wait=false >/dev/null 2>&1
      echo "  ok(CR)    $vol"; continue ;;
    CR-ONLY-no-zfs-on-node)
      # No agent runs on this node, so nothing will ever clear the finalizer.
      # Delete async and report rather than block the whole run on it.
      kubectl --context="$CTX" -n "$ns" delete zfsvolume "$vol" --ignore-not-found --wait=false >/dev/null 2>&1
      sleep 2
      if cr_exists "$ns" "$vol"; then
        echo "  STUCK     $vol: no agent on $node, finalizer will not clear"
        STUCK+=("$ns/$vol"); FAIL=$((FAIL+1))
      else
        echo "  ok(CR)    $vol (node $node has no ZFS)"
      fi
      continue ;;
  esac

  if [ "${HOST_OK[$node]:-no}" != yes ]; then
    echo "  SKIP      $vol: node $node has no ZFS, dataset cannot be verified"; FAIL=$((FAIL+1)); continue
  fi

  # Destroy the dataset BEFORE deleting the CR. The reverse order hangs: the
  # node agent's delete handler runs a bare `zfs destroy`, which fails on a
  # dataset that has snapshots, so it never removes `zfs.openebs.io/finalizer`
  # and `kubectl delete` blocks forever. Destroying first leaves the agent
  # nothing to do but drop the finalizer.
  if ds_exists "$node" "$ds"; then
    m=$(ssh -n -o BatchMode=yes "$node" "$ZP; zfs get -H -o value mounted '$ds' 2>/dev/null")
    if [ "$m" = yes ]; then echo "  SKIP      $vol became MOUNTED mid-run"; FAIL=$((FAIL+1)); continue; fi
    if ssh -n -o BatchMode=yes "$node" "$ZP; zfs destroy $rflag'$ds'"; then
      echo "  destroyed $vol"
    else
      echo "  FAIL      $vol: zfs destroy $rflag failed"; FAIL=$((FAIL+1)); continue
    fi
  fi

  kubectl --context="$CTX" -n "$ns" delete zfsvolume "$vol" --ignore-not-found --wait=false >/dev/null 2>&1
  for _ in $(seq 1 15); do cr_exists "$ns" "$vol" || break; sleep 2; done
  if cr_exists "$ns" "$vol"; then
    echo "  STUCK     $vol: CR still Terminating after 30s (finalizer not cleared)"
    STUCK+=("$ns/$vol"); FAIL=$((FAIL+1))
  else
    echo "  ok        $vol (dataset + CR removed)"
  fi
done

if [ ${#STUCK[@]} -gt 0 ]; then
  echo
  echo "These CRs are stuck Terminating because nothing cleared"
  echo "zfs.openebs.io/finalizer. Their datasets are already gone, so removing the"
  echo "finalizer is safe - but it is a mutation, so run it yourself:"
  for s in "${STUCK[@]}"; do
    echo "  kubectl --context=$CTX -n ${s%%/*} patch zfsvolume ${s##*/} --type=merge -p '{\"metadata\":{\"finalizers\":null}}'"
  done
fi

echo
echo "=============================== POST STATE ================================"
while read -r n; do
  [ "${HOST_OK[$n]:-no}" = yes ] && ssh -n -o BatchMode=yes "$n" "$ZP; zpool list -o name,alloc,size,cap" 2>/dev/null | sed "s/^/  [$n] /"
done <<< "$NODES"
echo "  ZFSVolume CRs : $(kubectl --context="$CTX" get zfsvolumes.zfs.openebs.io -A --no-headers 2>/dev/null | wc -l)"
echo "  PVs           : $(kubectl --context="$CTX" get pv --no-headers 2>/dev/null | wc -l)  (must be unchanged)"
echo "  PVCs          : $(kubectl --context="$CTX" get pvc -A --no-headers 2>/dev/null | wc -l)  (must be unchanged)"
echo "  failures      : $FAIL"
echo "==========================================================================="
[ "$FAIL" -gt 0 ] && exit 1
exit 0
