# Swap Role

Configures swap space on a Linux system.

## Requirements

- Root/sudo access
- Supported: Ubuntu/Debian

## Role Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `swap_file_path` | `/swapfile` | Path to the swap file |
| `swap_file_size_mb` | `16384` | Size of swap file in MB |
| `swap_swappiness` | `10` | vm.swappiness value (0-100) |
| `swap_sysctl_file` | `/etc/sysctl.d/99-swap.conf` | Path to sysctl config file |

## Dependencies

None.

## Example Playbook

```yaml
- hosts: servers
  roles:
    - role: swap
      vars:
        swap_file_size_mb: 8192
        swap_swappiness: 10
```

## Tags

All tasks use the `swap` tag for selective execution:

```bash
ansible-playbook playbook.yml --tags swap
```
