# Major page faults

## Summary

`grigri` server is suffering a high rate of major page faults since it was reinstalled.

## Timeline

2023-08-21

    - 22:57:XX CEST - Ubuntu 22.04 installed.
    - 23:34:07 CEST - Added to the cluster as worker.

2023-08-22

    - 06:XX:00 CEST - Max ~500 major page faults per second. 95% of memory utilization.
    - 18:40:XX CEST - `grigri` becomes K3s server.

## Root cause analysis

High memory usage is causing this major page faults. We still have to investigate why 95% seems the
threshold for this system.
