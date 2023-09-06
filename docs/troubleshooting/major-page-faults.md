# Major page faults

## Summary

`grigri` server is suffering a high rate of major page faults since it was reinstalled.

## Timeline

2023-08-21:

    - 22:57:XX CEST - Ubuntu 22.04 installed.
    - 23:34:07 CEST - Added to the cluster as worker.

2023-08-22:

    - 06:XX:XX CEST - Max ~500 major page faults per second. 95% of memory utilization.
    - 18:40:XX CEST - `grigri` becomes K3s server.

2023-09-05:

    - Reduce `zfs_arc_max` to keep under 90% of memory usaged. This fixed major page faults.

2023-09-06:

    - 09:14:XX CEST - Increase `zfs_arc_max` and set up `zfs_arc_sys_free` (3.2Gi) to try to avoid having free memory but still ensuring that major page faults are not happening any more.
    - 12:30:XX CEST - Increase `zfs_arc_sys_free` to 4Gi cause memory available is ~ 1.5Gi.
    - 16:22:XX CEST - Remove `zfs_arc_sys_free` and decrease `zfs_arc_max` to 12Gi.

## Root cause analysis

High memory usage is causing this major page faults. We still have to investigate why 95% seems the
threshold for this system.
