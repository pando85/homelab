# Add or remove nodes

Or how to scale vertically. To replace the same node with a clean OS, remove it and add it again.

## Add new nodes

!!! tip

    You can add multiple nodes at the same time

Add nodes details to the inventory **at the end of the group** (kube_control_plane or kube_node):

```diff
diff --git a/metal/inventory/hosts.ini b/metal/inventory/hosts.ini
--- a/metal/inventory/hosts.ini
+++ b/metal/inventory/hosts.ini
@@ -15,6 +15,7 @@
 [amd64_node]
 grigri
+k8s-amd64-2
```

Setup OS and network: [manual Setup](../deployment/manual-setup.md)

Join the cluster:

```bash
make metal
```

That's it!

## Remove a node

!!! danger

    It is recommended to remove nodes one at a time

!!! warning

    Removing a host from `metal/inventory/hosts.ini` only stops Ansible from targeting it — it
    does not stop k3s on the box itself. If the machine is still running with
    `/etc/rancher/node/password` on disk, it will silently rejoin the cluster on the next boot.
    Always drain, uninstall k3s on the node, and delete the Node object **before** touching the
    inventory.

1. Drain the node:

   ```sh
   kubectl drain ${NODE_NAME} --delete-emptydir-data --ignore-daemonsets --force
   ```

2. Uninstall k3s **on the node itself** (still in the inventory at this point):

   ```sh
   cd metal
   ANSIBLE_EXTRA_ARGS="--limit ${NODE_NAME}" make uninstall-k3s
   ```

   `metal/playbooks/uninstall/k3s.yml` stops/disables the `k3s` service, kills leftover
   container/kubelet processes, unmounts `/run/k3s` and `/var/lib/rancher/k3s`, and removes
   `/usr/local/bin/k3s`, `/etc/rancher/k3s`, `/etc/rancher/node` (the cluster join token) and
   the systemd unit. This repo installs k3s from a downloaded binary, so upstream's
   `k3s-agent-uninstall.sh`/`k3s-killall.sh` do not exist on the node — this playbook is the
   equivalent.

3. Remove the node from the cluster (this also garbage-collects its
   `<node>.node-password.k3s` secret in `kube-system`):

   ```sh
   kubectl delete node ${NODE_NAME}
   ```

4. Now remove it from the inventory, and delete its `host_vars/` file if it has one:

   ```diff
   diff --git a/metal/inventory/hosts.ini b/metal/inventory/hosts.ini
   --- a/metal/inventory/hosts.ini
   +++ b/metal/inventory/hosts.ini
   @@ -14,7 +14,6 @@
    [amd64_node]
    grigri
   -k8s-amd64-2
   ```

   Commit and let ArgoCD/Ansible converge.

5. Shutdown the node:

   ```
   ssh root@${NODE_IP} poweroff
   ```
