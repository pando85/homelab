# Import ZFS dataset

## Steps

- stop app
- clone volume
- create new volume
- change volume for app
- check app
- remove old volume

## Commands

In ZFS server:

```
# variables
PVC=datasets/k8s/l/v/pvc-b98464a7-6df2-43a9-bd06-326d3aa2255d
NAME=jellyseerr-config-zfs
SIZE=1G
NAMESPACE=jellyseerr

PVC_NAME=${NAME}
DATASET=datasets/openebs
NEW_DATASET=${DATASET}/${NAME}

zfs snapshot ${PVC}@clone
zfs clone ${PVC}@clone ${NEW_DATASET}
zfs promote ${NEW_DATASET}
zfs unmount ${NEW_DATASET}
zfs set mountpoint=legacy ${NEW_DATASET}
zfs set quota=${SIZE} ${NEW_DATASET}

cat << EOF > /tmp/pv.yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: ${NAME}
spec:
  accessModes:
  - ReadWriteOnce
  capacity:
    storage: ${SIZE}i # size of the volume
  claimRef:
    apiVersion: v1
    kind: PersistentVolumeClaim
    name: ${PVC_NAME}
    namespace: ${NAMESPACE}
  csi:
    driver: zfs.csi.openebs.io
    fsType: zfs
    volumeAttributes:
      openebs.io/poolname: ${DATASET}
    volumeHandle: ${NAME}
  nodeAffinity:
    required:
      nodeSelectorTerms:
      - matchExpressions:
        - key: kubernetes.io/hostname
          operator: In
          values:
          - grigri
  persistentVolumeReclaimPolicy: Delete
  storageClassName: openebs-zfspv
  volumeMode: Filesystem
EOF
k3s kubectl apply -f /tmp/pv.yaml

cat << EOF > /tmp/zfs-volume.yaml
apiVersion: zfs.openebs.io/v1
kind: ZFSVolume
metadata:
  finalizers:
  - zfs.openebs.io/finalizer
  name: ${NAME}
  namespace: zfs-localpv
spec:
  capacity: "$(echo "${SIZE::-1} * 1024 * 1024 * 1024" | bc)" # size of the volume in bytes
  fsType: zfs
  ownerNodeID: grigri
  shared: "yes"
  poolName: ${DATASET}
  volumeType: DATASET
status:
  state: Ready
EOF
k3s kubectl apply -f /tmp/zfs-volume.yaml
```
