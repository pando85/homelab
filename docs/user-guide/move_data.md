# Move data

## Import ZFS dataset

### Steps

- stop app
- clone volume
- create new volume
- change volume for app
- check app
- remove old volume

### Commands

In ZFS server:

```bash
# variables
PVC=datasets/k8s/l/v/pvc-0f17e0bf-6741-44fa-9e37-e5ed394ff56b
NAME=transcoder-rabbit
SIZE=1G
NAMESPACE=transcoder

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
  capacity: "$(echo "(${SIZE::-1} * 1024 * 1024 * 1024) / 1" | bc)" # size of the volume in bytes
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

## Move ZFS volume between nodes

### Steps

- Stop app
- Send snapshots
- Create new volume
- Change volume for app
- Check app
- Remove old volume

I'm not sure how to handle PVC Bound. At the moment I just marked the PV as Retain and recreate the PVC manually.

### Commands

In ZFS server:

```bash
# variables
PVC_NAME=minio-backup
SIZE=2000G
NAMESPACE=minio
NODE_NAME=grigri

DATASET=datasets/openebs
NEW_DATASET=${DATASET}/${PVC_NAME}

zfs set mountpoint=legacy ${NEW_DATASET}
zfs set quota=${SIZE} ${NEW_DATASET}

cat << EOF > /tmp/pv.yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: ${PVC_NAME}
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
    volumeHandle: ${PVC_NAME}
  nodeAffinity:
    required:
      nodeSelectorTerms:
      - matchExpressions:
        - key: kubernetes.io/hostname
          operator: In
          values:
          - ${NODE_NAME}
  persistentVolumeReclaimPolicy: Retain
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
  name: ${PVC_NAME}
  namespace: zfs-localpv
spec:
  capacity: "$(echo "(${SIZE::-1} * 1024 * 1024 * 1024) / 1" | bc)" # size of the volume in bytes
  fsType: zfs
  ownerNodeID: ${NODE_NAME}
  shared: "yes"
  poolName: ${DATASET}
  volumeType: DATASET
status:
  state: Ready
EOF
k3s kubectl apply -f /tmp/zfs-volume.yaml
```
