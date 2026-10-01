# k8s-amd64-1 — Hardware Reference

## System Overview

| Component | Specification |
|-----------|---------------|
| **Model** | ASUS Mini PC PN51-E1 |
| **BIOS** | 0302 (2021-03-10) — see Key Takeaways |
| **CPU** | AMD Ryzen 5 5500U with Radeon Graphics, 6C/12T, 400–4057 MHz |
| **RAM** | 16 GB (2×8 GB DDR4-2400 SODIMM, both slots occupied) |
| **OS Disk** | 119.2 GB SATA SSD (SanDisk SD6SB1M-128G, S/N 141924401021) |
| **iGPU** | AMD Lucienne [1002:164c], driver `amdgpu` |
| **NIC** | Realtek RTL8125 2.5GbE [10ec:8125], driver `r8169` |
| **WiFi** | Intel Wireless 8265/8275 [8086:24fd], driver `iwlwifi` (unused, DOWN) |
| **OS** | Ubuntu 26.04.1 LTS, kernel 7.0.0-34-generic |
| **Role** | K3s untainted amd64 worker |

## Motherboard Resource Sharing

The PN51-E1 is a mini PC with no user-accessible PCIe slots or SATA backplane. The single M.2 slot
and the single 2.5″ SATA bay are hardwired; there are no bandwidth-sharing conflicts to manage.

## Current Storage Configuration

### SATA Drive

| Device | Model | Serial | Size | Transport | ROTA |
|--------|-------|--------|------|-----------|------|
| sda | SanDisk SD6SB1M- | 141924401021 | 119.2G | sata | 0 |

### Partition Layout

| Partition | Size | Type | Mount |
|-----------|------|------|-------|
| sda1 | 1G | vfat | `/boot/efi` |
| sda2 | 2G | ext4 | `/boot` |
| sda3 | 116.2G | LVM2_member | — |

LVM volume `ubuntu--vg-ubuntu--lv` spans the full 116.2G partition, ext4 filesystem is 114G with
~102G free. Already fully expanded at install time.

### RAM Population

| Slot | Bank | Size | Speed | Manufacturer | Part Number |
|------|------|------|-------|--------------|-------------|
| DIMM 0 | P0 CHANNEL A | 8 GB | DDR4-2400 | Unknown | CT8G4SFS824A.C8FP (Crucial) |
| DIMM 0 | P0 CHANNEL B | 8 GB | DDR4-2400 | Unknown | Unknown |

Both SODIMM slots are occupied. The physical memory array reports a **maximum capacity of 32 GB**.
Upgrading to 32 GiB requires replacing both sticks with 2×16 GB DDR4-3200 SODIMM.

**Note:** Installed memory runs at 2400 MT/s, not 3200. The Ryzen 5 5500U supports DDR4-3200; the
limiting factor is likely the installed modules (one is a Crucial CT8G4SFS824A.C8FP, rated 2400).

## Available Expansion

- **No M.2 slot free** — the single M.2 slot (if populated) is not in use on this unit; no NVMe
  was detected by `lsblk` or `lspci`
- **No SATA ports free** — single 2.5″ bay occupied by the OS SSD
- **No PCIe slots** — mini PC form factor
- **RAM upgrade path** — replace 2×8 GB with 2×16 GB DDR4-3200 SODIMM (max 32 GB per ASUS spec)

## Hardware Sensors

| hwmon | Name | Provides |
|-------|------|----------|
| hwmon0 | k10temp | CPU temperature |
| hwmon1 | r8169_0_200:00 | NIC temperature |
| hwmon2 | asus | ASUS EC (fan, board temp) |
| hwmon3 | iwlwifi_1 | WiFi temperature |
| hwmon4 | amdgpu | iGPU temperature |

## Verification Commands

```bash
# NIC driver and PCI ID
cat /sys/class/net/enp2s0/device/uevent
lspci -nnk | grep -A3 -i net

# iGPU
lspci -nnk | grep -A3 -i vga

# CPU details
nproc
lscpu | grep -E 'Model name|Thread|Core|MHz'

# Disk layout
lsblk -o NAME,MODEL,SERIAL,SIZE,TYPE,TRAN,ROTA

# BIOS version and system identity
sudo -n dmidecode -t bios | head -20
sudo -n dmidecode -t system | head -20
sudo -n dmidecode -t baseboard | head -20

# RAM population (slot count, speed, manufacturer)
sudo -n dmidecode -t memory

# Hardware monitoring sensors
for f in /sys/class/hwmon/hwmon*/name; do echo "$f: $(cat $f)"; done

# Full PCI topology
lspci -nn
```

## Official Documentation

- [ASUS Mini PC PN51-E1 Specifications](https://www.asus.com/mini-pcs-desktops/mini-pcs/asus-mini-pc-pn51-e1/)
- [ASUS Mini PC PN51-E1 Support (BIOS, Manual)](https://www.asus.com/mini-pcs-desktops/mini-pcs/asus-mini-pc-pn51-e1/helpdesk_bios/)

## Key Takeaways

1. **Untainted amd64 worker**, joined via `metal/inventory/hosts.ini` `[amd64_node]`. First node
   named per the `k8s-{hardware_tag}-{numerical_id}` convention; prusik and grigri predate it.

2. **No ZFS pool** — must never be added to `allowedTopologies` in
   `system/zfs-localpv/templates/storage-class-openebs-zfspv.yaml:13-18`. That exclusion is the only
   thing preventing durable PVCs from binding here.

3. **No bindable StorageClass** — k3s' bundled local-storage is disabled at
   `metal/roles/k3s/defaults/main.yml:8-12`, so this node can only host stateless workloads until a
   local-path class is added (TODO-6.3 in `planning/k8s-amd64-1-node-addition.md`).

4. **16 GiB RAM is the binding constraint** — the CI runner (`ci-runner-0`) peaks at 17.4 GiB and
   7.68 cores, so it cannot be moved here. Both SODIMM slots are occupied with 8 GB modules; a
   32 GiB upgrade (2×16 GB DDR4-3200 SODIMM) would make the CI runner migration viable. The physical
   memory array reports a 32 GB maximum.

5. **Memory runs at 2400 MT/s**, not 3200 — one stick is a Crucial CT8G4SFS824A.C8FP rated at 2400.
   A matched 2×16 GB DDR4-3200 kit would improve both capacity and bandwidth.

6. **CPU is weak single-thread Zen 2 at ~15 W TDP** — suited for latency-tolerant stateless work,
   not transcoding or ML. The iGPU (Lucienne) is present but not exposed to containers.

7. **2.5 GbE ceiling matters for ingress** — `externalTrafficPolicy: Local` with a BGP LB IP
   (`system/ingress-nginx/values.yaml:94-96`) means a replica on this node would add a hop and a
   bandwidth ceiling to Jellyfin streams served from prusik.

8. **BIOS version 0302 is from 2021-03-10** — there are documented reports of random freezes on the
   PN51-E1 that a BIOS update mitigates. If the node exhibits hangs, check for a newer BIOS before
   investigating software causes.
