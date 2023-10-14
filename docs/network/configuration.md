# Configuration

## Diagram

![](diagram.svg)

## pfSense

Manual configuration and backups in grigri. Check `metal/roles/grigri_setup/tasks/backup-user.yml`
file.

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
  g22   1   1   1   None   Admit All   Disable   Disable   0
  g23   1   1   1   None   Admit All   Disable   Disable   0
  g24   1   1   1,101,501-502   101,501-502   Admit All   Disable   Disable   0
  g25   1   1   1   None   Admit All   Disable   Disable   0
  g26   1   1   1   None   Admit All   Disable   Disable   0
  ```

  Summary:

  ```yaml
  port 1-12: DMZ (VLAN 101)
  port 13-23: LAN (VLAN 1/default)
  port 24: LAN (VLAN 1 - Untagged, VLAN101 - Tagged, VLAN 501 - Tagged, VLAN 502 - Tagged)
  ```

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
