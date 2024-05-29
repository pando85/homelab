# Migrate controller node

Based in [K3s backup and restore doc](https://docs.k3s.io/datastore/backup-restore).

## Procedure

1. Take etcd snapshot and stop k3s in old controller.
2. Copy `/var/lib/rancher/k3s/server` and etcd snapshot to new controller.
3. Update Ansible inventory controller node and start new controller.
4. Check API is working and there are no agents.
5. Start all agents with `make cluster`.

Example:

```bash
# k8s-amd64-1
k3s etcd-snapshot save
systemctl stop k3s

rsync -av --delete /var/lib/rancher/k3s/server/ backup@prusik:/datasets/backups/k3s/server

# grigri
k3s server --cluster-reset --cluster-reset-restore-path=/datasets/backups/k3s/server/db/snapshots/on-demand-grigri-1716963887 --token /datasets/backups/k3s/server/token

# change inventory with new controller and move controller to worker

# localhost
cd metal
ANSIBLE_EXTRA_ARGS="--limit prusik" make cluster
kubectl get nodes
make cluster

# remove labels
```
