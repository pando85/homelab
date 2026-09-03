# Readest UI Hardcoded OAuth Providers

## Problem

Readest's frontend has hardcoded OAuth provider buttons (Google, Apple, GitHub, Discord) in the authentication UI. The `OAuthProvider` type in `ProviderLogin.tsx` is restricted to these specific values:

```typescript
export type OAuthProvider = 'google' | 'apple' | 'azure' | 'github' | 'discord';
```

The UI doesn't support custom OAuth providers, and there's no runtime configuration to add them.

## Root Cause

Readest is designed as a SaaS product with specific OAuth providers. The self-hosted version doesn't include configuration for custom providers because the upstream project doesn't anticipate self-hosters using different OAuth providers.

## Attempted Solutions

### Custom Docker Image

**Rejected** - Building a custom Docker image for Readest would require:
- Maintaining a fork of the Readest repository
- Rebuilding the image on every Readest update
- Tracking upstream changes to the auth UI
- Significant maintenance burden

### Runtime Configuration

Readest doesn't support runtime configuration for OAuth providers. The `OAUTH_PROVIDERS` environment variable suggested in some discussions doesn't exist in the codebase.

### CSS/JavaScript Injection via Nginx

Tried injecting CSS/JavaScript via nginx `sub_filter` to hide unwanted buttons:

```nginx
sub_filter '</body>' '<script>setInterval(function(){document.querySelectorAll("button").forEach(function(b){if(b.textContent.includes("Sign in with Google")||b.textContent.includes("Sign in with Apple")||b.textContent.includes("Sign in with GitHub"))b.style.display="none"})},100);</script></body>';
```

**Failed** - Next.js SPA doesn't reliably execute injected scripts, and the buttons are rendered client-side after the initial HTML load.

### GoTrue Environment Variables

Tried disabling providers via GoTrue environment variables:

```yaml
GOTRUE_EXTERNAL_GOOGLE_ENABLED: "false"
GOTRUE_EXTERNAL_APPLE_ENABLED: "false"
GOTRUE_EXTERNAL_GITHUB_ENABLED: "false"
```

**Partial success** - This disables the backend OAuth providers, but the UI still shows the buttons. Clicking them results in an error from GoTrue.

## Workaround

Use an init container to patch the compiled JavaScript bundles at pod startup:

```yaml
initContainers:
  patch-oauth:
    image:
      repository: ghcr.io/readest/readest
      tag: 0.12.6
    command:
      - /bin/sh
      - -c
      - |
        set -e
        STATIC_DIR="/app/apps/readest-app/.next/static"
        PATCHED_DIR="/patched-static"
        
        echo "Copying static assets..."
        cp -r "${STATIC_DIR}/." "${PATCHED_DIR}/"
        
        echo "Patching OAuth provider..."
        PATCHED=0
        for f in $(find "${PATCHED_DIR}" -name "*.js" -type f); do
          if grep -q 'provider:"discord"' "$f" 2>/dev/null || grep -q '"Discord"' "$f" 2>/dev/null; then
            echo "Patching $f"
            sed -i 's/provider:"discord"/provider:"custom:kanidm"/g' "$f"
            sed -i 's/"Discord"/"Kanidm"/g' "$f"
            sed -i 's/(0,i\.jsx)(R,{provider:"google",handleSignIn:n,Icon:U,label:r("Sign in with {{provider}}",{provider:"Google"})}),//g' "$f"
            sed -i 's/(0,i\.jsx)(R,{provider:"apple",handleSignIn:n,Icon:O\.FaApple,label:r("Sign in with {{provider}}",{provider:"Apple"})}),//g' "$f"
            sed -i 's/(0,i\.jsx)(R,{provider:"github",handleSignIn:n,Icon:O\.FaGithub,label:r("Sign in with {{provider}}",{provider:"GitHub"})}),//g' "$f"
            PATCHED=$((PATCHED + 1))
          fi
        done
        
        if [ "$PATCHED" -eq 0 ]; then
          echo "ERROR: Could not find OAuth provider to patch"
          exit 1
        fi
        
        echo "Patched $PATCHED file(s)"
    volumeMounts:
      - name: patched-static
        mountPath: /patched-static

volumes:
  - name: patched-static
    emptyDir:
      sizeLimit: 500Mi
```

The patched static files are then mounted into the main container:

```yaml
containers:
  client:
    volumeMounts:
      - name: patched-static
        mountPath: /app/apps/readest-app/.next/static
```

## How It Works

1. The init container copies the compiled JavaScript bundles from the Readest image
2. It searches for files containing `provider:"discord"` or `"Discord"`
3. It uses `sed` to:
   - Replace `provider:"discord"` with `provider:"custom:kanidm"`
   - Replace `"Discord"` with `"Kanidm"` (button label)
   - Remove the Google, Apple, and GitHub button JSX
4. The patched files are stored in an emptyDir volume
5. The main container mounts the patched files, overriding the originals

## Impact

- **Maintenance burden**: The patch must be updated if Readest changes its UI structure or JavaScript compilation output
- **Fragile**: The sed patterns are specific to the current Readest version and may break on updates
- **Automatic**: The init container runs on every pod start, so changes are applied automatically
- **No custom image**: We don't need to maintain a fork of Readest

## Verification

Check that the patch was applied:

```bash
kubectl exec -n readest deployment/readest-client -c client -- \
  grep -r 'provider:"custom:kanidm"' /app/apps/readest-app/.next/static/
```

Check the logs:

```bash
kubectl logs -n readest deployment/readest-client -c patch-oauth
```

Expected output:

```
Copying static assets...
Patching OAuth provider...
Patching /patched-static/chunks/abc123.js
Patched 1 file(s)
```

## Future Improvements

If Readest adds support for custom OAuth providers via runtime configuration:
- Remove the init container
- Remove the nginx rewrite
- Use standard OAuth provider configuration

If Readest makes the OAuth provider list configurable:
- Update the init container to use the new configuration method
- Remove the sed patching logic

## References

- Readest source: `apps/readest-app/src/app/auth/components/AuthPanel.tsx`
- Readest source: `apps/readest-app/src/app/auth/components/ProviderLogin.tsx`
