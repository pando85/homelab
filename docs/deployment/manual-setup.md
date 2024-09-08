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
- _only for controller HA_ - Create lb for apiserver (used HaProxy: increase client, server and
  tunnel\* timeouts to 86400000)
- Add DNS entry

**tunnel\***: must be added in `backend->advanced settings->backend pass thru` as
`timeout tunnel 86400s`

## Servers

### Flash SDs

- odroid-hc4: [image](https://www.armbian.com/odroid-hc4/)
- amd64: [usb-stick](https://releases.ubuntu.com/22.04/)
- grigri: [usb-stick](https://releases.ubuntu.com/22.04/)
- prusik-ipmi: [image](https://files.pikvm.org/images/v4plus-hdmi-rpi4-latest.img.xz)

Use script from `scripts/prepare_sdcard.sh` to prepare instances. amd64 and grigri should be
installed manually.

#### amd64 instances

Installed with Ubuntu: select ubuntu server (**non minimized**) and follow the process.

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
crw------- 1 root root 90, 1 Nov  6 21:38 /dev/mtd0ro
odroidhc4:~:# flash_erase /dev/mtd0 0 0
Erasing 4 Kibyte @ fff000 -- 100 % complete
odroidhc4:~:#
```

#### Naming convention

All nodes must be named with prefix `k8s-{hardware_tag}-{numerical_id}`. For example:

- k8s-odroid-hc4-1
- k8s-amd64-1

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

Use playbook `playbooks/install/so.yml` to setup servers.

```bash
make first-boot
```

**Note**: Armbian default user/password -> root/1234

## Cluster

`playbooks/install/cluster.yml` to setup Kubernetes.

```bash
ansible-playbook playbooks/install/k8s.yml --become
```
