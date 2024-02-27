# Restore backup

## ZFS

```bash
zfs rollback ${ZFS_VOLUME}@{ZFS_SNAPSHOT}
```

## Postgres

If after restore there are this kind of errors:

> My wal position exceeds maximum replication lag

Replica can be promoted to leader using `patronictl failover` command as root.

## Longhorn

- scale down replicas and delete pvc
- restore latest backup from [UI](https://longhorn.k8s.grigri/#/backup):
  - pv with the same name
  - pvc with the same name
- scale up and recreate pvc. Warning: change storageClass to `longhorn-static`
