# Ansible compatibility with Ubuntu 26.04 nodes

## Problem

Adding an x86 worker (ASUS PN51-E1) running **Ubuntu Server 26.04.1** to a fleet of Ubuntu 22.04
(`grigri`) and 24.04 (`prusik`). The node had to be provisionable by `metal/` without changing the
behaviour of the existing nodes.

Investigation found three hard blockers, two latent variable-precedence defects, and one
pre-existing `make` failure. Several other suspected 26.04 breakages turned out to be non-issues.

## Root Cause

### Blockers

1. **Managed-node Python 3.14.** Ubuntu 26.04 ships `python3` 3.14 (there is no `python3.13` in
   resolute). `metal/requirements.txt` pinned `ansible-core~=2.17`, whose managed-node support tops
   out at Python 3.12; 2.18/2.19 reach 3.13. Python 3.14 needs **ansible-core ≥ 2.20**.
   The pin was also already stale and self-contradictory: `ansible~=12.2` resolves to ansible 12.3.0,
   which requires `ansible-core~=2.19.5`, so `pip install -r requirements.txt` could not satisfy both
   lines. ansible-core 2.17 has been EOL since 2025-11-30.

2. **`sudo-rs` is the default sudo provider on 26.04.** Ansible's `sudo` become plugin invokes
   `sudo -H -S -n …`. sudo-rs **removed `-H`** (upstream #567/#568), and `-S` without a TTY fails with
   "A terminal is required to authenticate" (#1668, filed against Ubuntu 26.04). sudo-rs' prompt
   format also differs, and ansible-core's matching regex exists only on `devel` with backports still
   open (#86964, #87620-22, tracking issue #85837). Classic sudo is still installed at
   `/usr/bin/sudo.ws`.

3. **The `ntp` package no longer exists in resolute.** Only `chrony` (4.8, main, now the default time
   daemon) and `ntpsec` (1.2.3, universe) are available.

### Latent defects these exposed

4. **`ntp_daemon` could never be set from inventory.** `geerlingguy.ntp` sets it in
   `vars/<os_family>.yml`, loaded by `include_vars` (precedence 18), which beats inventory
   `host_vars` (9). `host_vars/prusik.yml` therefore had a dead `ntp_daemon: ntpsec` line; prusik
   only worked because ntpsec ships an `ntp.service` alias unit.

   Static role params do **not** help either. Measured, not assumed:

   | override mechanism | precedence | result |
   |---|---|---|
   | inventory `host_vars` | 9 | loses |
   | `roles:` list `vars:` (static role params) | 20 | **loses** |
   | `include_role` `vars:` | 20/21 | wins |
   | `-e` extra vars | 22 | wins |

   A hardcoded static role param was still overridden by the inner role's `include_vars`, so the only
   inventory-driven fix is a wrapper role that uses `include_role`. That is `metal/roles/ntp/`.

5. **`make requirements-ansible` exited 1.** `roles/geerlingguy.ntp` is gitignored
   (`metal/roles/.gitignore:1`) and galaxy-installed. When the directory exists without
   `.galaxy_install_info`, `ansible-galaxy install` refuses to replace it and exits 1 — breaking
   `prepare`, `cluster`, `first-boot`, `uninstall-k3s` and `console`, which all depend on that target.
   Confirmed identical on ansible-core 2.19.12 and 2.20.9, so it predates the upgrade.

6. **`ansible-lint` was silently broken on the control node.** The venv runs Python 3.14.7 and
   `ansible_compat` refuses to start:
   `RuntimeError: Python 3.14 requires ansible-core version >= 2.20.0, and we found 2.19.12.`
   Every existing lint finding was therefore invisible. Fixing the pin surfaced 39 pre-existing
   findings; `ansible.cfg` also sets `deprecation_warnings = False`, which hides upgrade signals.

### Verified non-issues

- **`dnsutils`** is a *virtual* package in resolute, provided by `bind9-dnsutils`, so
  `apt install dnsutils` still works and `prepare_basic_packages` needs no change. This avoided
  introducing the repo's first distro-version conditional.
- **rust-coreutils** is the default on 26.04, but the only `mount -o remount,rw /` in the repo is
  gated on `ansible_host == "prusik-ipmi"` (`roles/prepare/tasks/main.yml:5-13`).
- **`apt-key` removal / deb822 default**: nothing in `metal/` uses `apt-key`, and the two one-line
  `.list` repos (gVisor, NVIDIA) are still accepted by APT 3.
- **`/tmp` as tmpfs** (systemd 259): k3s and containerd write to `/var/lib/rancher/k3s` and
  `/usr/local/bin`. The only `/tmp` consumers are control-node-side (`metal/ansible.cfg:5`,
  `roles/zfs_exporter/tasks/main.yml:19`).
- **cgroup v1 removal / containerd 2.2.1**: K3s is cgroup v2 and bundles its own containerd.
- **No distro conditionals existed** in `metal/` (all gating is on `ansible_architecture` or
  `ansible_hostname`), and `roles/prepare/tasks/python.yml` resolves the interpreter path dynamically,
  so Python 3.14 needs no `ansible_python_interpreter`.

## How to Diagnose

```bash
cd metal
# control-node toolchain vs. what the pins claim
.direnv/python-*/bin/ansible --version | head -1
.direnv/python-*/bin/pip list | grep -iE '^ansible'
# does lint even start?
.direnv/python-*/bin/ansible-lint --version
# does the galaxy step succeed? (exit 1 == the vendored-role collision)
. direnv/bin/activate 2>/dev/null; make -n requirements-ansible
# managed-node facts for an existing host
ansible prusik -m setup -a 'filter=ansible_distribution_version'
ansible prusik -m setup -a 'filter=ansible_python_version'
```

## Fix / Workaround

Applied in this repo:

- `metal/requirements.txt`: `ansible~=13.0`, `ansible-core~=2.20`, `ansible-lint~=26.9`.
  Verified installable on control-node Python 3.14.7 → ansible 13.8.0 / ansible-core 2.20.9 /
  ansible-lint 26.9.0. Managed-node support 3.9-3.14 still covers jammy (3.10) and noble (3.12).
  `community.general` 8.5.0 and `community.crypto` 2.18.0 were deliberately **not** bumped —
  `community.general.random_string` generates the k3s cluster token
  (`roles/k3s/tasks/main.yml:57`), so it is not a dependency to move casually. It was re-tested
  under core 2.20 and still returns a 32-char string.
- `metal/roles/ntp/`: wrapper role that injects `ntp_daemon` as an `include_role` param from
  `ntp_daemon_select` (default `ntp`). `metal/playbooks/install/cluster.yml` now calls `role: ntp`.
  The vendored `geerlingguy.ntp` stays **byte-identical to upstream 2.0.0** — verified with
  `diff -r` against a fresh galaxy install — because it is gitignored and a patch to it would not
  survive a clone.
- `metal/Makefile`: `--ignore-errors` on the role install, with a comment. Never use `--force` there.
- `metal/inventory/host_vars/prusik.yml`: dropped the dead `ntp_daemon: ntpsec` line. Behaviour is
  unchanged (the effective unit stays `ntp`, ntpsec's alias). Set `ntp_daemon_select: ntpsec` to
  manage the real unit instead.
- `roles: - name: X` → `- role: X` in `cluster.yml` and `prepare.yml`, clearing two
  `schema[playbook]` lint findings.

Host vars a 26.04 node needs:

```yaml
# sudo-rs: make Ansible use the classic sudo binary that is still installed
ansible_become_exe: /usr/bin/sudo.ws

# chrony is the default daemon on 26.04; the `ntp` package does not exist
ntp_daemon_select: chrony
ntp_package: chrony
ntp_config_file: /etc/chrony/chrony.conf
```

`ntp_package` and `ntp_config_file` do work from `host_vars` — the role resolves them with a
`set_fact … when not defined` idiom. Only `ntp_daemon` needed the wrapper.

Bootstrap order on a fresh 26.04 node (`make first-boot` cannot be used — it forces
`ansible_user=root --ask-pass`, and Ubuntu images are `PermitRootLogin prohibit-password`):
create the user and `~/.ssh/authorized_keys`, add
`<user> ALL=(ALL:ALL) NOPASSWD:ALL` to `/etc/sudoers.d/90-ansible` (mode 440), then run
`ANSIBLE_EXTRA_ARGS="--limit <node>" make prepare`.

## Verification performed (no 26.04 node available yet)

| check | result |
|---|---|
| `pip install` of the new pins, control node Python 3.14.7 | resolves; ansible 13.8.0 / core 2.20.9 / lint 26.9.0 |
| `ansible-playbook --syntax-check` on every playbook in `metal/playbooks/` | passes on **both** core 2.19.12 and 2.20.9 |
| deprecation warnings with `ANSIBLE_DEPRECATION_WARNINGS=True` | none |
| daemon resolution, simulated 22.04 / 24.04 / 26.04 inventories | `ntp` / `ntpsec` / `chrony`, identical on both toolchains |
| rendered template selection | `ntp.conf.j2` for 22.04+24.04, `chrony.conf.j2` for 26.04; both exist |
| `--tags ntp` propagation through the wrapper | inner role executes |
| vendored role vs. upstream 2.0.0 | byte-identical |
| `community.general.random_string` (k3s token) under core 2.20 | returns 32 chars |
| `ansible-lint --profile safety` | 39 → 37 findings, **zero introduced**; `roles/ntp` passes the stricter `production` profile |
| `pre-commit run --files <changed>` | all hooks pass |
| `ansible-galaxy install … --ignore-errors` | exit 0, vendored role untouched |

The daemon-resolution evidence comes from a connection-free harness that replays the role's own
variable tasks (`include_vars` + guarded `set_fact`) against a synthetic inventory, plus probe roles
for each override mechanism. Nothing was run against `grigri` or `prusik`.

## Still unverified — needs the real node

- `/usr/bin/sudo.ws` exists and `sudo.ws -H -S -n true` succeeds.
- `force_apt_get: true` under APT 3.1 (`roles/prepare/tasks/main.yml:50`).
- gVisor `runsc` on kernel 7.0 — the `gvisor` role runs on every non-aarch64 node.
- AppArmor 5.0.0~beta1 with the role's `apparmor=0` + disabled-service approach; this also removes
  snap confinement, so nothing on the node may depend on snapd.
- Overwriting `/etc/chrony/chrony.conf` from `chrony.conf.j2` drops Ubuntu's
  `sourcedir /etc/chrony/sources.d` include, so the node's only time source becomes `pfsense.grigri`
  with no fallback. Same single-source posture prusik has today; add the `sourcedir` line to the
  template if that is ever a problem.
- A live `--check --diff` run of `make cluster` against `grigri`/`prusik` under core 2.20. Syntax and
  variable resolution are proven, but no real host has been touched.

## Known remaining defect (deliberately not fixed)

`ntp_driftfile` in `host_vars/prusik.yml` is dead for the same precedence reason as `ntp_daemon`
was: `vars/Debian.yml` sets it via `include_vars`. prusik's rendered `/etc/ntp.conf` therefore still
contains `driftfile /var/lib/ntp/drift` rather than `/var/lib/ntpsec/ntp.drift`. Fixing it changes a
live control-plane config file and restarts NTP, so it was left alone. To fix, pass
`ntp_driftfile: "{{ ntp_driftfile_select | default('/var/lib/ntp/drift', true) }}"` through
`roles/ntp/tasks/main.yml` the same way `ntp_daemon` is passed.
