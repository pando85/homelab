# Restore backup

## ZFS

```bash
# check snapshot
zfs list datasets/k8s/l/v/${PV_NAME} -t snapshot -o name,creation
zfs rollback ${ZFS_VOLUME}@{ZFS_SNAPSHOT}
```

## Postgres

If after restore there are this kind of errors:

> My wal position exceeds maximum replication lag

Replica can be promoted to leader using `patronictl failover` command as root.
