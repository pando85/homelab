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

rsync -av --delete -e "ssh -i /root/id_ed25519" /var/lib/rancher/k3s/server/ \
    backup@grigri.grigri:/datasets/backups/k8s/server
rsync -av -e "ssh -i /root/id_ed25519" \
    /var/lib/rancher/k3s/server/db/snapshots/on-demand-k8s-amd64-1-1692721476 \
    backup@grigri.grigri:/datasets/backups/k8s/

# grigri
k3s server --cluster-reset --cluster-reset-restore-path=/datasets/backups/k8s/on-demand-k8s-amd64-1-1692721476 --token /datasets/backups/k8s/server/token

# localhost
cd metal
ANSIBLE_EXTRA_ARGS="--limit grigri" make cluster
kubectl get nodes
make cluster
```
