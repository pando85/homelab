# prusik — Hardware Reference

## System Overview

| Component | Specification |
|-----------|---------------|
| **Motherboard** | ASUS PRIME X670-P |
| **CPU** | AMD Ryzen 9 7950X |
| **RAM** | 64GB DDR5 |
| **OS Disk** | 512GB NVMe (SanDisk Extreme 500GB) |
| **Cache** | 2TB NVMe (FIKWOT FN960) |
| **Data Disks** | 4× 12TB SATA (ST12000NM0127) in RAIDZ |
| **GPU** | NVIDIA GeForce GTX 1060 3GB |
| **OS** | Ubuntu 24.04 |
| **Role** | K3s agent node |

## Motherboard Resource Sharing

The ASUS PRIME X670-P has **6 SATA ports** and **3 M.2 slots**. Resource sharing is hardware-based and automatic.

### Critical Rule: PCIEX1 and SATA Ports

**PCIEX1 (PCIe 3.0 x1) shares bandwidth with SATA6G_3 and SATA6G_4.**

- When PCIEX1 is **empty**: All 6 SATA ports (SATA6G_1 through SATA6G_6) are available
- When PCIEX1 is **populated**: SATA6G_3 and SATA6G_4 are disabled

### No Sharing for Other Slots

The following do **not** disable any SATA ports:

- **PCIEX16_1** (PCIe 4.0 x16) — occupied by GTX 1060
- **PCIEX16_2** (PCIe 4.0 x16) — empty
- **PCIEX16_3** (PCIe 4.0 x16) — empty
- **M.2_1** (PCIe 5.0 x4, CPU lanes) — occupied by 2TB NVMe
- **M.2_2** (PCIe 4.0 x4, CPU lanes) — occupied by 465GB NVMe
- **M.2_3** (PCIe 4.0 x4, chipset lanes) — empty

### Resource Sharing Matrix

| Slot/Port | Status | SATA Impact |
|-----------|--------|-------------|
| PCIEX16_1 | GTX 1060 installed | None |
| PCIEX16_2 | Empty | None |
| PCIEX16_3 | Empty | None |
| **PCIEX1** | **Empty** | **If populated: disables SATA6G_3/4** |
| M.2_1 | 2TB NVMe (CPU lanes) | None |
| M.2_2 | 465GB NVMe (CPU lanes) | None |
| M.2_3 | Empty | None |

## Current Storage Configuration

### SATA Drives

| Device | Model | Serial | Size | Controller | by-path |
|--------|-------|--------|------|------------|---------|
| sda | ST12000NM0127 | ZJV59K2D | 10.9T | 0d:00.0 | pci-0000:0d:00.0-ata-3 |
| sdb | ST12000NM0127 | ZJV5Y9G5 | 10.9T | 0f:00.0 | pci-0000:0f:00.0-ata-1 |
| sdc | ST12000NM0127 | ZJV5WPAZ | 10.9T | 0f:00.0 | pci-0000:0f:00.0-ata-2 |
| sdd | ST12000NM0127 | ZJV5SKEF | 10.9T | 0f:00.0 | pci-0000:0f:00.0-ata-4 |

### NVMe Drives

| Device | Model | Serial | Size | PCIe Path |
|--------|-------|--------|------|-----------|
| nvme0n1 | FIKWOT FN960 2TB | AA240141547 | 1.8T | 00:01.2 → 02:00.0 (M.2_1, CPU lanes) |
| nvme1n1 | SanDisk Extreme 500GB | 23414Q803583 | 465.8G | 00:02.2 → 10:00.0 (M.2_2, CPU lanes) |

### Available Expansion

- **2 SATA ports free** (SATA6G_5 and SATA6G_6)
- **PCIEX1 empty** — can be used without disabling SATA ports (but see note below)
- **M.2_3 empty** — PCIe 4.0 x4 available

**Note:** If you install a device in PCIEX1, you will lose SATA6G_3 and SATA6G_4. Since 4 of 6 SATA ports are already in use, populating PCIEX1 would reduce available SATA ports to 0.

## Verification Commands

### Check SATA Port Availability

```bash
# List all ATA ports and their status
for host in /sys/class/ata_port/ata*; do
    echo "=== $(basename $host) ==="
    readlink -f "$host/device"
done

# List connected SATA devices with paths
ls -l /dev/disk/by-path/ | grep ata

# List all block devices
lsblk -o NAME,MODEL,SERIAL,SIZE,TRAN,HCTL
```

### Check PCIe Slot Occupancy

```bash
# Full PCIe topology
sudo lspci -tv

# SATA and NVMe controllers
sudo lspci -nnk | grep -i -A3 'sata\|ahci\|nvme'
```

### Check for SATA Errors

```bash
# Kernel messages related to SATA
sudo dmesg -T | grep -Ei 'ata|sata|ahci|error|failed|reset|link'

# Journal logs
sudo journalctl -k -b | grep -Ei 'ata|sata|ahci'
```

## Official Documentation

- [ASUS PRIME X670-P Specifications](https://www.asus.com/motherboards-components/motherboards/prime/prime-x670-p/)
- [ASUS PRIME X670-P Manual (PDF)](https://dlcdnets.asus.com/pub/ASUS/mb/Socket%20AM5/PRIME%20X670-P/E20186_PRIME_X670-P_UM_WEB.pdf)

## Key Takeaways

1. **PCIEX1 is the only slot that affects SATA ports** — it disables SATA6G_3/4 when populated
2. **M.2 slots do not disable any SATA ports** — all three M.2 slots can be used simultaneously with all 6 SATA ports
3. **Current system has 2 SATA ports available** — can add 2 more SATA drives
4. **Avoid populating PCIEX1** unless you're willing to lose 2 SATA ports (which would leave 0 available)
5. **Resource sharing is hardware-based** — no BIOS setting can override it
