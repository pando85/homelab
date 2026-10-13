# Kubernetes upgrade

Update `k3s_version` to desired version and then run:

```bash
cd metal
ANSIBLE_EXTRA_ARGS="-t k3s-upgrade -e serial=1" make cluster
```

*Note*: it worked perfectly with serial=100% or running it by default.

*Stale tag*: no task or role in `metal/` is tagged `k3s-upgrade`, so `-t k3s-upgrade` selects
nothing. The `k3s` role is tagged `k3s` (`metal/playbooks/install/cluster.yml:14-16`) and
deliberately leaves upgrades to the system-upgrade-controller Plans (`system/system-upgrade/k3s/`) —
see the comment in `metal/roles/k3s/tasks/main.yml:12`. K3s version bumps stay human-only per
`AGENTS.md`, and agents may apply only a committed, config-only `-t k3s --limit <node>` change.
