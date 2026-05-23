# Kata Containers: containerd Drop-in Directory

## Problem

After upgrading kata-deploy (3.31.0+), the pod crashes with:

```
Error: K3s/RKE2: rendered config at /etc/containerd/config.toml does not import the drop-in dir
'config-v3.toml.d'. kata-deploy requires the containerd template to include that import.
Add e.g. imports = [".../config-v3.toml.d/*.toml"] to the template and restart k3s/RKE2.
```

## Root Cause

Kata-deploy 3.31.0 switched from writing a single static file
(`/opt/kata/containerd/config.d/kata-deploy.toml`) to a "drop-in directory" pattern. It now:

1. Checks that the containerd config template imports `config-v3.toml.d/*.toml`
2. Writes individual runtime configs into that drop-in directory at runtime

The old containerd template imported the static kata path directly, which no longer satisfies
the new validation check.

## Fix

The containerd template (`config-v3-runtimes.toml.tmpl`) must use the drop-in glob import:

```toml
imports = ["/var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.d/*.toml", ...]
```

And the drop-in directory must exist on the node before kata-deploy starts.

### Files Changed

- `metal/roles/k3s/files/config-v3-runtimes.toml.tmpl` — `imports` line uses `config-v3.toml.d/*.toml` glob
- `metal/roles/k3s/tasks/main.yml` — task added to create `config-v3.toml.d` directory on amd64 nodes

### Apply

```bash
cd metal && ANSIBLE_EXTRA_ARGS="-t k3s" make cluster
```

This re-runs the k3s Ansible role which updates the containerd template and creates the drop-in
directory. After k3s restarts, kata-deploy will pass its validation check and write runtime configs
into the drop-in directory.
