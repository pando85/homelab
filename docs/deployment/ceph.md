# Ceph

## Tools

```bash
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph -s
kubectl -n rook-ceph exec deploy/rook-ceph-tools -it -- bash
```

## Troubleshooting

PG_autoscaler is broken when not all pools contains a device class. Error message: `contains an overlapping root`

Following [issue](https://github.com/rook/rook/issues/11764) define `.mgr` pool manually:

```bash
kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph osd pool rm .mgr .mgr --yes-i-really-really-mean-it
kubectl -n rook-ceph apply -f system/rook-ceph/templates/ceph-block-pool-mgr.yaml
```
