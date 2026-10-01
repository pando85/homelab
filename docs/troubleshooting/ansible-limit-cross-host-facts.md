# Scoped Ansible Runs Break On Cross-Host Gathered Facts

## Problem

Adding a node with the scoped form AGENTS.md requires:

```bash
cd metal && ANSIBLE_EXTRA_ARGS="--limit k8s-amd64-1" make cluster
```

fails partway through the k3s role:

```
TASK [k3s : Copy k3s config files] *****
[ERROR]: Task failed: object of type 'HostVarsVars' has no attribute 'ansible_hostname'
Origin: metal/roles/k3s/templates/config.yaml.j2
```

An unscoped `make cluster` succeeds, so the bug is invisible until someone scopes a run — which is
now the only permitted form.

A second, silent failure follows from the same run: the time daemon keeps its **distro** config in
memory. On the new node `chronyc sources` showed `canonical.com` pools while
`/etc/chrony/chrony.conf` correctly contained `server pfsense.grigri iburst`.

## Root Cause

**1. Cross-host gathered facts.** `config.yaml.j2` and the kubeconfig rewrite both read
`hostvars[groups['kube_control_plane'][0]].ansible_hostname`. `ansible_hostname` is a *gathered*
fact, not an inventory variable. Under `--limit` the control plane is in no play, so its facts are
never gathered, and `hostvars['prusik']` holds inventory vars only.

`delegate_to` does **not** collect facts for the delegate — with the default `delegate_facts: false`
facts are attributed to the inventory host. So the two `slurp` tasks delegated to prusik did not
populate them either.

The configured fallback did not help: `ansible.cfg` sets `fact_caching = jsonfile` with
`fact_caching_connection = /tmp`, but `/tmp/prusik` did not exist (tmp reaping, and the cache is only
written for hosts whose facts were gathered). With no cache entry there is nothing to fall back to.

**2. Discarded handlers.** Handlers run at the end of the play. The k3s failure discarded the pending
chrony restart, so the daemon never reloaded the config that had already been written.

This one is sticky: on the next run the config file matches, the template task reports `ok` instead of
`changed`, no handler is notified, and the daemon keeps the stale config indefinitely. A green
Ansible run and a wrong running state.

## Fix

`1c3f5a48` — use the inventory name instead of the fact:

```jinja
server: https://{{ groups['kube_control_plane'][0] }}:6443
```

`groups['kube_control_plane'][0]` is `prusik`, which is what the fact resolved to, so the render is
byte-identical for existing nodes and matches the live cluster context (`https://prusik:6443`).

`baa2eb43` — flush handlers at the end of the `roles/ntp` wrapper so committed time config is applied
before k3s starts, regardless of what fails later:

```yaml
- name: Apply the time daemon configuration before continuing
  ansible.builtin.meta: flush_handlers
```

### Rule

Never read another host's **gathered** facts in `metal/`. Use the inventory name, an inventory
variable, or an explicit `setup:` with `delegate_facts: true`. Audit with:

```bash
grep -rn "hostvars\[" metal/ --include='*.yml' --include='*.j2' | grep -v '\.direnv'
```

Both known occurrences were fixed; this should stay empty apart from deliberate cases.

### Files Changed

- `metal/roles/k3s/templates/config.yaml.j2`
- `metal/roles/k3s/tasks/main.yml`
- `metal/roles/ntp/tasks/main.yml`

## Apply

A node already left with a stale time daemon needs one restart, since its config file is unchanged
and will not re-notify the handler:

```bash
cd metal && .direnv/python-3.14/bin/ansible k8s-amd64-1 -i inventory/hosts.ini \
  -m systemd -a 'name=chrony state=restarted' --become
```

Then confirm it moved off the internet pools:

```bash
ssh k8s-amd64-1 'chronyc sources'   # expect 192.168.192.1, matching prusik and grigri
```

## Verification

Before mutating, prove the render with a scoped check run:

```bash
cd metal && ANSIBLE_EXTRA_ARGS="--limit <node> -t k3s --check --diff" make cluster
```

The diff must show `server: https://prusik:6443`.

## Related

- `docs/user-guide/add-or-remove-nodes.md` — the full node-addition runbook
- `docs/troubleshooting/ansible-ubuntu-2604-compat.md` — the `include_vars` precedence trap, a
  different way the same galaxy role ignores inventory variables
- `planning/k8s-amd64-1-node-addition.md` — the run this was found in
