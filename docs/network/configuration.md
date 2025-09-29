# Configuration

## Diagram

![](diagram.svg)

## pfSense

Manual configuration and backups in prusik. Backup user is created in
`metal/roles/setup/tasks/backup-user.yml` and backup is configured (partly manually) on this file
`metal/playbooks/install/backups.yml`.

## Disaster Recovery

A backup for disaster recovery is stored in pass:

```bash
pass grigri/pfsense.config.xml > /tmp/pfsense.config.xml
```

For reinstalling PfSense, you will need to connect the null modem serial RS232 cable and connect
with Linux:

```bash
sudo picocom -b 115200 /dev/ttyUSB0
```

**Note**: `screen` could be used too.

You will need to use a PfSense USB installer image and configure PPPoE WAN to boot.

PPPoE is on `igb0` interface and credentials are in pass `grigri/digi_pppoe`.

If you cannot install it in this way you could install it from an offline ISO image from
[PfSense old images](https://atxfiles.netgate.com/mirror/downloads/).

For creating a bootable USB stick from ISO you can use the
[Etcher tool](https://www.balena.io/etcher/).

After download the image, create the USB stick and connect it to the router, you will need to
connect to the serial console and follow the installation steps.

Then you can restore the configuration from the XML file.

## gs724t

- `Ipv4 Network Interface Configuration -> IP Configuration`:

  ```yaml
  - current_network_configuration_protocol: static
    ip_address: 192.168.76.10
    subnet_mask: 255.255.255.0
    default_gateway: 192.168.76.254
  ```

- `Switching -> VLAN`:

  - Add `101` DMZ
  - Add `501` IoT
  - Add `502` IoT Offline

- `Switching -> VLAN -> Advanced -> VLAN Membership`:

  ```
  VLAN ID: 1
  VLAN Name: Default
  VLAN Type: Default
  Port  1   2   3   4   5   6   7   8   9   10   11   12   13   14   15   16   17   18   19   20   21   22   23   24
                                                           U    U    U    U    U    U    U    U    U    U    U    U
  ```

  ```
  VLAN ID: 101
  VLAN Name: DMZ
  VLAN Type: Static
  Port  1   2   3   4   5   6   7   8   9   10   11   12   13   14   15   16   17   18   19   20   21   22   23   24
        U   U   U   U   U   U   U   U   U   U    U    U
  ```

  ```
  VLAN ID: 501
  VLAN Name: IoT
  VLAN Type: Static
  Port  1   2   3   4   5   6   7   8   9   10   11   12   13   14   15   16   17   18   19   20   21   22   23   24
                                                                                                        T    T    T
  ```

  ```
  VLAN ID: 502
  VLAN Name: IoT Offline
  VLAN Type: Static
  Port  1   2   3   4   5   6   7   8   9   10   11   12   13   14   15   16   17   18   19   20   21   22   23   24
                                                                                                        T    T    T
  ```

- `Switching -> VLAN -> Advanced -> Port PVID Configuration`:

  ```
  g1   101   101   101   None   Admit All   Disable   Disable   0
  g2   101   101   101   None   Admit All   Disable   Disable   0
  g3   101   101   101   None   Admit All   Disable   Disable   0
  g4   101   101   101   None   Admit All   Disable   Disable   0
  g5   101   101   101   None   Admit All   Disable   Disable   0
  g6   101   101   101   None   Admit All   Disable   Disable   0
  g7   101   101   101   None   Admit All   Disable   Disable   0
  g8   101   101   101   None   Admit All   Disable   Disable   0
  g9   101   101   101   None   Admit All   Disable   Disable   0
  g10   101   101   101   None   Admit All   Disable   Disable   0
  g11   101   101   101   None   Admit All   Disable   Disable   0
  g12   101   101   101   None   Admit All   Disable   Disable   0
  g13   1   1   1   None   Admit All   Disable   Disable   0
  g14   1   1   1   None   Admit All   Disable   Disable   0
  g15   1   1   1   None   Admit All   Disable   Disable   0
  g16   1   1   1   None   Admit All   Disable   Disable   0
  g17   1   1   1   None   Admit All   Disable   Disable   0
  g18   1   1   1   None   Admit All   Disable   Disable   0
  g19   1   1   1   None   Admit All   Disable   Disable   0
  g20   1   1   1   None   Admit All   Disable   Disable   0
  g21   1   1   1   None   Admit All   Disable   Disable   0
  g22   1   1   1,501-502   501-502   Admit All   Disable   Disable   0
  g23   1   1   1   None   Admit All   Disable   Disable   0
  g24   1   1   1,101,501-502   101,501-502   Admit All   Disable   Disable   0
  g25   1   1   1   None   Admit All   Disable   Disable   0
  g26   1   1   1   None   Admit All   Disable   Disable   0
  ```

  Summary:

  ```yaml
  port 1-12: DMZ (VLAN 101)
  port 13-21: LAN (VLAN 1/default)
  port 22-23: LAN (VLAN 1 - Untagged, VLAN 501 - Tagged, VLAN 502 - Tagged)
  port 24: LAN (VLAN 1 - Untagged, VLAN101 - Tagged, VLAN 501 - Tagged, VLAN 502 - Tagged)
  ```
