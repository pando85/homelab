# Import ZFS dataset

In ZFS server:

```
# variables
PVC=datasets/k8s/l/v/pvc-eb3b9543-b268-4e10-8c5a-bb2f204c4115
NAME=mosquitto-tls
SIZE=2G
NAMESPACE=mosquitto-tls

DATASET=datasets/openebs
NEW_DATASET=${DATASET}/${NAME}

zfs snapshot ${PVC}@clone
zfs clone ${PVC}@clone ${NEW_DATASET}
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
    name: ${NAME}
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
  namespace: ${NAMESPACE}
spec:
  capacity: "$((${SIZE::-1} * 1024 * 1024 * 1024))" # size of the volume in bytes
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
