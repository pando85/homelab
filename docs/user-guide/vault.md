# Vault Migration: File to Raft Storage

Step-by-step guide to migrate Vault from file storage to raft storage using
`vault operator migrate`.

## Overview

The `vault operator migrate` command converts data in-place from file to raft format, preserving
everything:

- KV secrets
- PKI root CA keys
- Auth methods
- Policies

## 1. Backup Data

Stop Vault and backup the PVC data (guarantees consistency):

```bash
# Scale down vault to stop writes
kubectl --context=grigri -n vault scale statefulset vault --replicas=0

# Wait for pod to terminate
kubectl --context=grigri -n vault wait --for=delete pod/vault-0 --timeout=120s

# Create backup pod mounting the PVC
cat <<EOF | kubectl --context=grigri apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: vault-backup
  namespace: vault
spec:
  containers:
  - name: backup
    image: busybox
    args: [sleep, "1000000"]
    volumeMounts:
    - name: vault-data
      mountPath: /data-source
  volumes:
  - name: vault-data
    persistentVolumeClaim:
      claimName: vault-file-vault-0
EOF

# Wait for backup pod
kubectl --context=grigri -n vault wait --for=condition=ready pod/vault-backup --timeout=60s

# Create backup tarball
kubectl --context=grigri -n vault exec vault-backup -- tar czf /tmp/vault-backup.tar.gz -C /data-source .

# Copy backup to local machine
kubectl --context=grigri -n vault cp vault-backup:/tmp/vault-backup.tar.gz ./vault-backup.tar.gz

# Cleanup backup pod
kubectl --context=grigri -n vault delete pod vault-backup
```

## 2. Migrate Data Format

Start Vault temporarily to run the migration:

```bash
# Scale vault back up
kubectl --context=grigri -n vault scale statefulset vault --replicas=1
kubectl --context=grigri -n vault wait --for=condition=ready pod/vault-0 --timeout=120s

# Get root token
TOKEN=$(kubectl --context=grigri -n vault get secret vault-unseal-keys -o jsonpath='{.data.vault-root}' | base64 -d)

# Create migration config and run migration
kubectl --context=grigri -n vault exec vault-0 -- sh -c "
  cat > /tmp/migrate.hcl << 'EOF'
storage_source \"file\" {
  path = \"/vault/file\"
}
storage_destination \"raft\" {
  path = \"/vault/file\"
  node_id = \"vault-0\"
}
cluster_addr = \"https://vault.vault:8201\"
EOF

  VAULT_SKIP_VERIFY=true VAULT_TOKEN=$TOKEN \
  vault operator migrate -config=/tmp/migrate.hcl
"
```

Expected output:

```
Migration successfully completed!
```

## 3. Update Vault Configuration

Edit `platform/vault/templates/vault.yaml`:

```yaml
config:
  storage:
    raft:
      path: "${ .Env.VAULT_STORAGE_FILE }"
      node_id: "vault-0"
  listener:
    tcp:
      address: "0.0.0.0:8200"
      tls_cert_file: /vault/tls/server.crt
      tls_key_file: /vault/tls/server.key
      cluster_address: "0.0.0.0:8201"
  api_addr: "https://vault.vault:8200"
  cluster_addr: "https://vault.vault:8201"
  ui: true
```

Commit and push (ArgoCD will sync):

```bash
git add platform/vault/templates/vault.yaml
git commit -m "feat(vault): migrate from file to raft storage"
git push
```

## 4. Redeploy Vault

```bash
# Delete pod to pick up new config
kubectl --context=grigri -n vault delete pod vault-0

# Wait for vault to be ready
kubectl --context=grigri -n vault wait --for=condition=ready pod/vault-0 --timeout=120s
```

## 5. Verify Migration

```bash
TOKEN=$(kubectl --context=grigri -n vault get secret vault-unseal-keys -o jsonpath='{.data.vault-root}' | base64 -d)

kubectl --context=grigri -n vault exec vault-0 -- sh -c "
  export VAULT_SKIP_VERIFY=true VAULT_TOKEN=$TOKEN

  echo '=== Vault Status ==='
  vault status

  echo ''
  echo '=== KV Secrets ==='
  vault kv list secret/

  echo ''
  echo '=== PKI Mounts ==='
  vault secrets list | grep pki

  echo ''
  echo '=== Raft Snapshot Test ==='
  vault operator raft snapshot save /tmp/test.snap && echo 'Raft snapshot OK'
"
```

Expected status output:

```
Storage Type             raft
HA Enabled               true
Raft Committed Index     XXX
Raft Applied Index       XXX
```

## ArgoCD Considerations

Vault modifies pod labels dynamically (`vault-active`, `vault-sealed`, etc.). ArgoCD may try to sync
these back. Add to your Application manifest:

```yaml
spec:
  ignoreDifferences:
    - kind: Pod
      name: vault-0
      jsonPointers:
        - /metadata/labels/vault-active
        - /metadata/labels/vault-sealed
        - /metadata/labels/vault-initialized
```

## Quick Reference

```bash
# Get root token
kubectl --context=grigri -n vault get secret vault-unseal-keys -o jsonpath='{.data.vault-root}' | base64 -d

# Check Vault status
kubectl --context=grigri -n vault exec vault-0 -- vault status

# Create raft snapshot
kubectl --context=grigri -n vault exec vault-0 -- sh -c "
  VAULT_SKIP_VERIFY=true VAULT_TOKEN=<token> \
  vault operator raft snapshot save /vault/file/backup.snap
"

# Restore raft snapshot
kubectl --context=grigri -n vault exec vault-0 -- sh -c "
  VAULT_SKIP_VERIFY=true VAULT_TOKEN=<token> \
  vault operator raft snapshot restore /vault/file/backup.snap
"

# Force leader step-down (for HA)
kubectl --context=grigri -n vault exec vault-0 -- sh -c "
  VAULT_SKIP_VERIFY=true VAULT_TOKEN=<token> vault operator step-down
"
```
