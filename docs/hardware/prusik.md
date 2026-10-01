# prusik — Hardware Reference

## System Overview

| Component | Specification |
|-----------|---------------|
| **Motherboard** | ASUS PRIME X670-P |
| **CPU** | AMD Ryzen 9 7950X |
| **RAM** | 64GB DDR5 |
| **OS Disk** | 512GB NVMe (SanDisk Extreme 500GB) |
| **Cache** | 2TB NVMe (FIKWOT FN960) |
| **Data Disks** | 4× 12TB SATA in RAIDZ (3× ST12000NM0127 + 1× MG07ACA12TEY) |
| **Fast Pool** | 2× 240GB SATA SSD (SanDisk SDSSDHII240G) in mirror |
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
| SATA6G_1-6 | All 6 ports occupied | N/A |

## Current Storage Configuration

### SATA Drives

| Device | Model | Serial | Size | Controller | by-path |
|--------|-------|--------|------|------------|---------|
| sda | MG07ACA12TEY | 91K0A0C0F9BG | 10.9T | 2:0:0.0 | pci-0000:0d:00.0-ata-3 |
| sdb | SDSSDHII240G | 170234400122 | 240G | 3:0:0.0 | pci-0000:0f:00.0-ata-1 |
| sdc | ST12000NM0127 | ZJV5Y9G5 | 10.9T | 6:0:0.0 | pci-0000:0f:00.0-ata-2 |
| sdd | ST12000NM0127 | ZJV5WPAZ | 10.9T | 7:0:0.0 | pci-0000:0f:00.0-ata-3 |
| sde | SDSSDHII240G | 170235401310 | 240G | 8:0:0.0 | pci-0000:0f:00.0-ata-4 |
| sdf | ST12000NM0127 | ZJV5SKEF | 10.9T | 9:0:0.0 | pci-0000:0f:00.0-ata-5 |

### NVMe Drives

| Device | Model | Serial | Size | PCIe Path |
|--------|-------|--------|------|-----------|
| nvme0n1 | FIKWOT FN960 2TB | AA240141547 | 1.8T | 00:01.2 → 02:00.0 (M.2_1, CPU lanes) |
| nvme1n1 | SanDisk Extreme 500GB | 23414Q803583 | 465.8G | 00:02.2 → 10:00.0 (M.2_2, CPU lanes) |

### Available Expansion

- **0 SATA ports free** (all 6 ports occupied)
- **PCIEX1 empty** — can be used but will disable SATA6G_3 and SATA6G_4 (see note below)
- **M.2_3 empty** — PCIe 4.0 x4 available

**Note:** If you install a device in PCIEX1, you will lose SATA6G_3 and SATA6G_4. Since all 6 SATA ports are already in use, populating PCIEX1 would disable 2 drives.

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
3. **All 6 SATA ports are occupied** — no expansion without PCIEX1 (which would disable 2 ports)
4. **Avoid populating PCIEX1** unless you're willing to lose 2 SATA ports (which would disable 2 drives)
5. **Resource sharing is hardware-based** — no BIOS setting can override it
