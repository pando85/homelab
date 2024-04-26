# Restore backup

## ZFS

```bash
zfs rollback ${ZFS_VOLUME}@{ZFS_SNAPSHOT}
```

## Postgres

If after restore there are this kind of errors:

> My wal position exceeds maximum replication lag

Replica can be promoted to leader using `patronictl failover` command as root.

## Velero

### Restoring to a Different Node

To restore to a different node, create a ConfigMap with the following YAML:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  # any name can be used; Velero uses the labels (below)
  # to identify it rather than the name
  name: change-pvc-node-selector-config
  # must be in the velero namespace
  namespace: velero
  # the below labels should be used verbatim in your
  # ConfigMap.
  labels:
    # this value-less label identifies the ConfigMap as
    # config for a plugin (i.e. the built-in restore item action plugin)
    velero.io/plugin-config: ""
    # this label identifies the name and kind of plugin
    # that this ConfigMap is for.
    velero.io/change-pvc-node-selector: RestoreItemAction
data:**Warning**: ArgoCD labels are going to be restored too. If you are doing the restore into other namespace as in the example, disable ArgoCD or modify the labels.
  # add 1+ key-value pairs here, where the key is the old
  # node name and the value is the new node name.
  grigri: prusik
```

### Restoring with a Different Storage Class

To restore with a different storage class, create a ConfigMap with the following YAML:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  # any name can be used; Velero uses the labels (below)
  # to identify it rather than the name
  name: change-storage-class-config
  # must be in the velero namespace
  namespace: velero
  # the below labels should be used verbatim in your
  # ConfigMap.
  labels:
    # this value-less label identifies the ConfigMap as
    # config for a plugin (i.e. the built-in restore item action plugin)
    velero.io/plugin-config: ""
    # this label identifies the name and kind of plugin
    # that this ConfigMap is for.
    velero.io/change-storage-class: RestoreItemAction
data:
  # add 1+ key-value pairs here, where the key is the old
  # storage class name and the value is the new storage
  # class name.
  openebs-zfspv: backup
```

**Warning**: Ensure that the original parent zfs volume exists in the target storage class (e.g., `datasets/openebs`).

```bash
velero restore create --from-backup ${BACKUP_NAME} --include-namespaces ${NAMESPACE} --restore-volumes=true --namespace-mappings ${NAMESPACE}:${TARGET_NAMESPACE}
```

**Note**: ArgoCD labels will also be restored. If restoring to a different namespace, consider disabling ArgoCD or modifying the labels accordingly.
