# OpenEBS ZFS Local PV: Slow Pod Startup with fsGroup

## Problem

Pods using `fsGroupChangePolicy: OnRootMismatch` with OpenEBS ZFS Local PV (`openebs-zfspv`
StorageClass, `fstype: zfs`) experience slow startup. `kubectl describe pod` shows:

```
Warning  VolumePermissionChangeInProgress  Setting volume ownership for <path>
is taking longer than expected, consider using OnRootMismatch
```

The kubelet recursively walks the entire volume on every pod start, even though
`fsGroupChangePolicy: OnRootMismatch` is configured in the pod security context.

## Root Cause

The kubelet (`pkg/volume/volume_linux.go`) skips the recursive chown only when **all three**
conditions are met on the volume root directory:

1. GID matches `fsGroup`
2. Permissions are a superset of required (0770 for rw)
3. **setgid bit is set** (`chmod g+s`)

OpenEBS ZFS CSI driver creates datasets without the setgid bit and with insufficient
permissions (typically 0700). The kubelet's `fsGroup` pass applies `chmod(info.Mode()|mask)`
which adds setgid + group permissions, but on the **next pod start** the root directory is
back to `0700` without setgid — the kubelet resets it during every mount cycle.

### Why `chmod 2770` doesn't persist

We attempted to manually set `chmod 2770` on the volume root after the kubelet finished its
permission pass. This appeared to work (`2770 drwxrws---`), but on the next pod restart the
kubelet re-applied its fsGroup pass and reset the root directory back to `0700 drwx------`.
The kubelet's volume ownership logic runs at mount time on every pod start, overwriting any
manual permission changes.

This is a known limitation: [kubernetes/kubernetes#105435][#105435],
[kubernetes/kubernetes#133892][#133892]. The CSI driver has no `fsGroupPolicy` field in
its CSIDriver spec and no StorageClass parameter for default permissions.

[#105435]: https://github.com/kubernetes/kubernetes/issues/105435
[#133892]: https://github.com/kubernetes/kubernetes/issues/133892

## How to Diagnose

1. Check events for `VolumePermissionChangeInProgress` warnings:
   ```bash
   kubectl describe pod <pod> -n <ns> | grep -A2 VolumePermissionChangeInProgress
   ```

2. Check the volume root permissions via the ZFS node daemonset on the target node:
   ```bash
   ZFS_POD=$(kubectl get pod -n zfs-localpv -o jsonpath='{.items[?(@.spec.nodeName=="<node>")].metadata.name}')
   PVC_UID=$(kubectl get pvc <pvc-name> -n <ns> -o jsonpath='{.metadata.uid}')
   POD_UID=$(kubectl get pod <pod> -n <ns> -o jsonpath='{.metadata.uid}')

   kubectl exec -n zfs-localpv $ZFS_POD -c openebs-zfs-plugin -- \
     stat /var/lib/kubelet/pods/$POD_UID/volumes/kubernetes.io~csi/$PVC_UID/mount
   ```

3. Expected failing state:
   ```
   Access: (0700/drwx------)  Uid: (10000)   Gid: (10000)
   ```
   Missing: setgid bit (`s` in group permissions) and 0770 mode.

4. Check all PVC mounts on a node at once:
   ```bash
   kubectl exec -n zfs-localpv $ZFS_POD -c openebs-zfs-plugin -- sh -c \
     'for m in $(mount | grep "type zfs" | grep -v subpaths | grep "/host/var/lib/kubelet" | sed "s|.*on /host\(/var/lib/kubelet[^ ]*\).*|\1|"); do stat -c "%a %A %U:%G %n" "$m" 2>/dev/null; done'
   ```
   Look for entries with mode `700` and no setgid (`drwx------` vs `drwxrws---`).

## Fix

### Best: remove `fsGroup` when the app manages its own ownership

If the application already runs as the volume owner (or an init stage chowns files to the
correct UID), `fsGroup` is unnecessary. Removing it eliminates the kubelet's recursive
permission walk entirely:

```yaml
defaultPodOptions:
  securityContext: {}
```

This applies when:
- The container starts as root and an init system (e.g. s6-overlay) chowns data to the
  final UID before dropping privileges
- The app runs as the same UID that owns the volume files
- The volume files are already owned by the correct user

### Fallback: init container for apps that truly need fsGroup

If the app requires `fsGroup` (e.g. multiple processes with different UIDs sharing a volume),
the init container approach can set permissions before the kubelet's pass. This is fragile
and may still not work reliably with `fstype: zfs`.

## Notes

- `acltype=posixacl` and `xattr=sa` are already inherited from the `datasets` ZFS parent
  — these are unrelated to the setgid issue.
- ZFS has no property to set default Unix permission bits or setgid on new datasets.
- The OpenEBS ZFS CSI driver does not support `fsGroupPolicy` in its CSIDriver spec,
  so kubelet handles all fsGroup changes.
- Kata Containers runtime is not the cause, but adds latency on top of the already-slow
  recursive walk.
