# Hermes Instance Config Overwrite Recovery

## Problem

When deploying a new Hermes instance, copying configuration files from an existing instance
overwrote the target instance's original config, memories, and MCP configurations. The instance
appeared to work but lost its unique identity and data.

## Root Cause

The deployment process involved copying `/opt/data/config.yaml`, `/opt/data/auth.json`, and
`/opt/data/.env` from an existing instance to the new instance. This overwrote the target
instance's configuration without checking if it already had its own config.

Key factors:
- The `config.yaml.bak-*` files are CLI configs, not gateway configs
- Hermes instances have unique memories, MCPs, and personalities that shouldn't be copied
- ZFS snapshots provide daily backups that can be used for recovery

## How to Diagnose

Check if the instance already exists and has its own configuration:

```bash
# Check if instance exists
kubectl --context=grigri get pod -n hermes-N

# Check if config exists
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- ls -la /opt/data/config.yaml

# Check memories
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- ls -la /opt/data/memories/

# Check skills/MCPs
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- ls /opt/data/skills/
```

## Fix / Workaround

### Prevention

When deploying a new Hermes instance:
1. **Check if instance already exists** before deploying
2. **Don't copy config files** from other instances unless explicitly needed
3. **Only copy auth.json** for API credentials (if needed)
4. **Create fresh config.yaml** with instance-specific settings:
   - Update `dashboard.public_url` to the new instance URL
   - Update `dashboard.oauth.self-hosted.issuer` to the new OAuth client
   - Configure instance-specific MCPs and skills

### Recovery from ZFS Snapshot

If config was overwritten, recover from ZFS snapshot:

```bash
# 1. Scale down the instance
kubectl --context=grigri scale statefulset hermes-N -n hermes-N --replicas=0

# 2. Find the PVC
kubectl --context=grigri get pvc -n hermes-N

# 3. List ZFS snapshots (on the node where PVC is bound)
ssh <node> "zfs list -t snapshot | grep <pvc-uuid>"

# 4. Destroy newer snapshots if needed
ssh <node> "sudo zfs destroy <dataset>@<newer-snapshot>"

# 5. Rollback to the snapshot
ssh <node> "sudo zfs rollback <dataset>@<snapshot>"

# 6. Scale back up
kubectl --context=grigri scale statefulset hermes-N -n hermes-N --replicas=1
```

### What NOT to Copy

- `/opt/data/config.yaml` — Contains instance-specific settings, personalities, MCPs
- `/opt/data/memories/` — Instance memories and knowledge
- `/opt/data/skills/` — Instance-specific skills and MCP configurations
- `/opt/data/state.db` — Instance state and session data

### What CAN be Copied

- `/opt/data/auth.json` — API credentials (if needed, but prefer creating fresh)
- `/opt/data/.env` — Environment variables (Telegram token, allowed users)

## Related

- [Hermes Agent Deployment](../deployment/hermes-agent.md)
- [ZFS Rollback](../user-guide/restore-backup.md#zfs)
