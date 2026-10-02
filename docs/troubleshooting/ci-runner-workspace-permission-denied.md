# CI Runner: `mkdir /workspace/<hash>: permission denied`

## Problem

Forgejo Actions jobs on the `ci-runner` (prusik) failed immediately after `workflow prepared`:

```
Runner 3a300011-… (version:v13.2.0) received task 100529 of job changes, triggered by event: pull_request
workflow prepared
mkdir /workspace/08eda103baa0d859: permission denied
```

A second, quieter symptom had been present since the same moment:

```
mkdir /cache/cache: permission denied
```

so `actions/cache` save and restore silently failed on every job.

The runner pod was `Running`, healthy, polling for tasks, and logging no errors — the failure was
reported only to the Forgejo job, never to the container log. Grepping runner logs for
`permission denied` returns nothing.

## Root Cause

Commit `7ce9ed37` migrated the prusik runner from three volumes to a single `fast-ci-zfspv` PVC with
`subPath` mounts. `/workspace` changed backing store:

| | before | after |
|---|---|---|
| volume | `emptyDir: {medium: Memory}` | `subPath: workspace` of a ZFS PVC |
| created by | kubelet, tmpfs, mode **0777** | CSI, fresh dataset, **root:root 0755** |
| writable by uid 1001 | yes | **no** |

The runner runs as `runAsUser: 1001` and, in host mode, does the `mkdir` itself. Against a
`drwxr-xr-x 0 0` parent, uid 1001 lands in "others" — `r-x`, no write — so `mkdir` returns EACCES.

Nothing else in the pod could have fixed the ownership:

- **No `fsGroup`.** It was deliberately removed in `bcf0c446` because the recursive chown made
  startup take minutes on `openebs-zfspv` — see
  [`openebs-zfspv-slow-startup-fsgroup.md`](openebs-zfspv-slow-startup-fsgroup.md).
- **`fsGroupChangePolicy: OnRootMismatch` was still set** — but it is meaningless without `fsGroup`,
  so it was dead config that read like a safeguard.
- **`supplementalGroups: [1001]`** adds gid 1001 to the process. It changes no file ownership, and
  `0755` grants the group only `r-x` anyway.
- **kubelet applies no fsGroup management to `subPath` directories** regardless, so re-adding
  `fsGroup` would not have been a reliable fix either.

## Why It Looked Intermittent

Two traps combined to hide a 100%-failure bug for ~9.5 hours:

1. **Only host-mode jobs were affected.** The runner registers `ubuntu-latest:host`,
   `ubuntu-24.04:host` and `docker`. The `:host` labels execute directly as uid 1001 against
   `host.workdir_parent: /workspace`. The `docker` label runs through the dockerd sidecar, which is
   `runAsUser: 0` — so container-mode jobs kept passing and masked the breakage.
2. **Both runners register an identical label set**, so Forgejo load-balanced host-mode jobs between
   prusik and `k8s-amd64-1`. Jobs landing on amd64 succeeded; jobs landing on prusik failed. The
   result read as a ~50% CI flake rate rather than a dead volume.

The amd64 runner was immune because it uses `hostPath` with `type: Directory` and Ansible
pre-creates `/srv/ci-runner/{workspace,cache}` as `1001:1001 0775`
(`metal/inventory/host_vars/k8s-amd64-1.yml`, tag `ci-runner-dirs`). Ownership lives in git there,
so kubelet's defaults never apply.

Confirming evidence: the prusik volume was 64 GiB with **128 KiB used (1%)** — no host-mode job had
ever succeeded on it.

## How to Diagnose

```bash
# Which runner is a UUID? Check each pod's registration file.
kubectl --context=grigri exec -n ci-runners <pod> -c runner -- \
  grep -o "<uuid-prefix>[^\"]*" /data/.runner

# The actual check: parent dir owner vs the uid doing the mkdir
kubectl --context=grigri exec -n ci-runners ci-runner-0 -c runner -- \
  sh -c 'id; ls -land /workspace /cache; test -w /workspace && echo WRITABLE || echo NOT-WRITABLE'

# Has the volume ever been used?
kubectl --context=grigri exec -n ci-runners ci-runner-0 -c runner -- df -h /workspace
```

Expect `drwxrwxr-x 1001 1001` and `WRITABLE`. `drwxr-xr-x 0 0` is the bug. Note that a `0%`/`1%`
used figure on a long-lived runner means host-mode execution has never worked — that is a stronger
signal than any log.

`test -w` uses `access(2)`, which reflects the real uid, so it is a genuine permission check that
writes nothing. To reproduce the failing syscall end to end, `mkdir` and `rmdir` a throwaway name
(never reuse a real workdir hash — a concurrent job may own it).

## Fix

A root init container that mounts the PVC **at its root, not via subPath**, and idempotently
creates/owns the two directories the uid-1001 runner needs:

```yaml
initContainers:
  - name: fix-volume-ownership
    image: busybox:1.38.0
    command: [sh, -c]
    args:
      - |
        set -eu
        for d in workspace cache; do
          mkdir -p "/data/$d"
          chown 1001:1001 "/data/$d"
          chmod 0775 "/data/$d"
        done
    securityContext:
      runAsUser: 0
    volumeMounts:
      - name: ci-runner-data
        mountPath: /data          # volume root - no subPath
```

Ordering works because init containers run before the main containers: kubelet's `subPath` setup
only creates a directory if it is missing, so it leaves the corrected ownership alone. The `docker`
subPath is deliberately left root-owned — only the `daemon` container (`runAsUser: 0`) uses it.

This is a one-time cost, unlike `fsGroup`, which re-walks the volume on every mount. It is the
"init container" fallback the fsGroup doc describes, and preferred here for that reason.

Also drop the dead `fsGroupChangePolicy` so it cannot be mistaken for a safeguard.

## General Rule

A non-root container needs write access to a **freshly provisioned** volume, and nothing in the pod
spec provides it, when any of these hold:

- the volume is a PVC with `subPath` mounts and there is no `fsGroup`
- `fsGroup` was removed for startup-latency reasons
- the volume is a `hostPath` — kubelet applies no fsGroup management to hostPath at all

For PVCs, use a root init container on the volume root. For hostPath, set owner and mode from
Ansible in `metal/` and use `type: Directory` — never `DirectoryOrCreate`, which silently creates a
root-owned directory and produces exactly this error. See
`planning/k8s-amd64-1-node-addition.md` TODO-6.3.

`emptyDir` (including `medium: Memory`) hides this class of bug entirely, because kubelet creates it
mode 0777. Migrating a non-root workload *off* an emptyDir onto a PVC or hostPath is therefore a
change that needs an explicit ownership plan — it is not a like-for-like volume swap.
