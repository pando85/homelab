# Manual setup <!-- omit from toc -->

- [Network](#network)
  - [Pfsense](#pfsense)
- [Servers](#servers)
  - [Flash SDs](#flash-sds)
    - [amd64 instances](#amd64-instances)
    - [odroid-hc4](#odroid-hc4)
      - [Bootloader Bypass Method](#bootloader-bypass-method)
    - [Naming convention](#naming-convention)
  - [Troubleshooting](#troubleshooting)
    - [Same mac problem](#same-mac-problem)
    - [No python interpreter found](#no-python-interpreter-found)
  - [Setup](#setup)
- [Cluster](#cluster)

## Network

### Pfsense

- Connect to DMZ `192.168.192.0/24`
- Add DHCP server: range(60-99), but fix agent IPs before add to the cluster.
- Add a **MAC-based DHCP reservation** for each node so the IP survives machine-id regeneration
  and reboots (k8s-amd64-1 kept 192.168.192.11 through both).
- The DMZ DHCP scope should push the `grigri` search domain (option 15/119). If it does not, set
  `prepare_dns_search_domains: [grigri]` in the node's `host_vars` instead.
- _only for controller HA_ - Create lb for apiserver (used HaProxy: increase client, server and
  tunnel\* timeouts to 86400000)
- Add DNS entry

**tunnel\***: must be added in `backend->advanced settings->backend pass thru` as
`timeout tunnel 86400s`

## Servers

### Flash SDs

- odroid-hc4: [image](https://www.armbian.com/odroid-hc4/)
- amd64: [usb-stick](https://releases.ubuntu.com/26.04/)
- grigri: [usb-stick](https://releases.ubuntu.com/22.04/)
- prusik-ipmi: [image](https://files.pikvm.org/images/v4plus-hdmi-rpi4-latest.img.xz)

Current OS per node:

| node | OS |
|---|---|
| prusik | Ubuntu 24.04 |
| grigri | Ubuntu 22.04 / 24.04 |
| k8s-amd64-1 | Ubuntu 26.04.1 |

Use script from `scripts/prepare_sdcard.sh` to prepare instances. amd64 and grigri should be
installed manually.

#### amd64 instances

Installed with Ubuntu: select ubuntu server (**non minimized**) and follow the process.

For a node with a single small SSD and no ZFS pool (e.g. k8s-amd64-1):

- Do **not** create a ZFS pool during install
- The installer may strand free extents in `ubuntu-vg`; expand afterwards with
  `lvextend -l +100%FREE -r /dev/ubuntu-vg/ubuntu-lv` (on k8s-amd64-1 the LV already spanned the
  VG — verify with `lsblk` and `df -h /`)
- Create the `agil` user (uid 1000) with an SSH key and install the OpenSSH server
- Skip "install security updates automatically" — Ansible owns unattended-upgrades
- Do not enable Ubuntu Pro or livepatch

#### odroid-hc4

**Important**: To be able to boot clean Armbian mainline based u-boot / kernel experiences, you need
to remove incompatible Petitboot loader that is shipped with the board.

##### Bootloader Bypass Method

This is now the preferred method. It is easier, and be performed without a display via SSH

> Install an SD Card with a fresh Armbian image Flip device upside down With a tool, press and hold
> down the black button. Continue holding button and plug in power to device Login to console or SSH
> and perform follow normal setup procedures Verify system can access SPI FLASH device and Erase
> Reboot

```bash
odroidhc4:~:# ls -ltr /dev/mtd*
crw------- 1 root root 90, 0 Nov  6 21:38 /dev/mtd0
brw-rw---- 1 root disk 31, 0 Nov  6 21:38 /dev/mtdblock0
crw------- 1 root root 90, 0 Nov  6 21:38 /dev/mtd0ro
odroidhc4:~:# flash_erase /dev/mtd0 0 0
Erasing 4 Kibyte @ fff000 -- 100 % complete
odroidhc4:~:#
```

#### Naming convention

All nodes must be named with prefix `k8s-{hardware_tag}-{numerical_id}`. For example:

- k8s-odroid-hc4-1
- k8s-amd64-1

`prusik` and `grigri` predate this convention. `k8s-amd64-1` is the first node that follows it.

Also, consider their use case and performance profile. For example, for Ceph nodes:

- k8s-sas-ssd-1
- k8s-hot-storage-2

### Troubleshooting

#### Same mac problem

Editing `/boot/ArmbianEnv.txt` didn't work.

`/etc/network/interfaces`:

```conf
...
auto eth0
iface eth0 inet dhcp
  hwaddress ether b6:09:a4:06:00:8b
```

#### No python interpreter found

```bash
ln -s /usr/bin/python3 /usr/bin/python
```

### Setup

!!! warning

    `make first-boot` is **not usable** for modern Ubuntu nodes: it forces
    `-e ansible_user=root --ask-pass`, and Ubuntu images are `PermitRootLogin prohibit-password`.
    It is also not scopeable (it appends its own `--limit` after `ANSIBLE_EXTRA_ARGS`, and the
    last `--limit` wins).

Before the first `make prepare`, bootstrap the new node manually:

1. Create the user and `~/.ssh/authorized_keys`
2. Add passwordless sudo — either add `/etc/sudoers.d/90-ansible` (mode 440) by hand, or run the
   first prepare with `-K` (ask-become-pass)

For Ubuntu 26.04 nodes, see
[`docs/troubleshooting/ansible-ubuntu-2604-compat.md`](../troubleshooting/ansible-ubuntu-2604-compat.md)
for `sudo-rs`, `chrony`, and Python 3.14 specifics.

For a worked example, see
[`planning/k8s-amd64-1-node-addition.md`](../../planning/k8s-amd64-1-node-addition.md).

Then run the scoped prepare:

```bash
cd metal && ANSIBLE_EXTRA_ARGS="--limit <node>" make prepare
```

**Note**: Armbian default user/password -> root/1234

## Cluster

`playbooks/install/cluster.yml` to setup Kubernetes.

```bash
cd metal && ANSIBLE_EXTRA_ARGS="--limit <node>" make cluster
```

!!! warning

    `make cluster` is on the AGENTS.md never-run list. An agent must hand it to a human even when
    scoped. With `--limit` the k3s role's `run_once` + `delegate_to: prusik` tasks are read-only
    (`slurp`) and the kubeconfig write is `delegate_to: localhost`.
