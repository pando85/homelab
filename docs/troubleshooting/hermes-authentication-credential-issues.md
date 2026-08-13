# Hermes Authentication and Credential Issues

## Problem

After deploying hermes-4, the instance consistently failed with HTTP 401 errors when trying to
use the isidoro/balanced model, even though:
- The API key was correct (verified with curl)
- The auth.json file contained the credential
- The config.yaml had the correct provider settings
- The same credential worked in hermes-2 and hermes-3

## Root Cause

Multiple issues combined to cause persistent authentication failures:

1. **Manual config.yaml editing lost fields** - Using Python to edit config.yaml dropped important
   nested fields (context_length, max_tokens, fallback_providers, etc.) that are required for
   proper provider configuration.

2. **auth.json base_url field was empty** - When credentials were added via `hermes auth add`,
   the base_url field was not populated. The provider needs this field to route requests correctly.

3. **state.db held stale credential cache** - The SQLite state database cached the broken
   credential state. Even after fixing auth.json, the gateway continued using the cached
   (broken) credentials.

4. **File permission issues** - When auth.json was edited as root, it became unreadable by the
   hermes user, causing "Permission denied" errors.

5. **pkill doesn't fully reset state** - Using `pkill -f "hermes gateway"` restarts the process
   but doesn't clear the in-memory credential cache or state.db.

## How to Diagnose

### Check credential configuration

```bash
# Verify auth.json structure and base_url
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- cat /opt/data/auth.json | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d['credential_pool']['custom:isidoro'], indent=2))"

# Check file permissions
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- ls -la /opt/data/auth.json

# Verify config.yaml has all required fields
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- cat /opt/data/config.yaml | grep -A 10 "^model:"
```

### Check for stale state

```bash
# Look for 401 errors in logs
kubectl --context=grigri logs -n hermes-N hermes-N-0 --tail=100 | grep -iE "(401|auth|error)"

# Check request dumps for actual API key being sent
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- ls -t /opt/data/sessions/request_dump_*.json | head -1 | \
  xargs kubectl --context=grigri exec -n hermes-N hermes-N-0 -- cat | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print(d['request']['headers']['Authorization'])"
```

### Test the credential directly

```bash
# Test API key with curl from inside the pod
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- \
  curl -s -H "Authorization: Bearer <api-key>" https://isidoro.grigri.cloud/v1/models
```

## Fix / Workaround

### 1. Copy working config from another instance

When config.yaml is corrupted or missing fields, copy the entire working config from another
instance:

```bash
# Export config from working instance
kubectl --context=grigri exec -n hermes-3 hermes-3-0 -- cat /opt/data/config.yaml > /tmp/config.yaml

# Import to broken instance
kubectl --context=grigri cp /tmp/config.yaml hermes-N/hermes-N-0:/opt/data/config.yaml
```

### 2. Ensure auth.json has base_url

The credential must have the base_url field populated:

```bash
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- python3 -c "
import json
data = json.load(open('/opt/data/auth.json'))
data['credential_pool']['custom:isidoro'][0]['base_url'] = 'https://isidoro.grigri.cloud/v1'
json.dump(data, open('/opt/data/auth.json', 'w'), indent=2)
"
```

### 3. Clear stale state database

Delete state.db to force a clean credential load:

```bash
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- \
  rm /opt/data/state.db /opt/data/state.db-wal /opt/data/state.db-shm
```

### 4. Fix file permissions

auth.json must be owned by hermes user with 600 permissions:

```bash
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- \
  chown hermes:hermes /opt/data/auth.json && chmod 600 /opt/data/auth.json
```

### 5. Delete pod for clean restart

**Don't use pkill** - it doesn't fully reset state. Delete the pod instead:

```bash
kubectl --context=grigri delete pod hermes-N-0 -n hermes-N
```

This ensures:
- In-memory credential cache is cleared
- state.db is reloaded from disk
- All services start fresh

### 6. Remove unused credentials

Having unused providers (like alibaba) in auth.json or .env creates confusion:

```bash
# Remove from auth.json
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- python3 -c "
import json
data = json.load(open('/opt/data/auth.json'))
data['credential_pool'].pop('alibaba-coding-plan', None)
json.dump(data, open('/opt/data/auth.json', 'w'), indent=2)
"

# Remove from .env
kubectl --context=grigri exec -n hermes-N hermes-N-0 -- \
  sed -i '/ALIBABA_CODING_PLAN_API_KEY/d' /opt/data/.env
```

## Prevention

When deploying new Hermes instances:

1. **Copy entire config.yaml** from a working instance instead of manual editing
2. **Verify auth.json structure** - ensure base_url is populated for all credentials
3. **Set correct permissions** - auth.json must be owned by hermes:hermes with 600 permissions
4. **Remove unused credentials** - keep auth.json and .env clean
5. **Use pod deletion** for restarts, not pkill
6. **Test immediately** - send a test message to verify the model works before considering
   deployment complete

## Related

- [Hermes Config Overwrite Recovery](hermes-config-overwrite-recovery.md)
- [Hermes Agent Deployment](../deployment/hermes-agent.md)
