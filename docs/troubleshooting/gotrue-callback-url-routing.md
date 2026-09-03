# GoTrue Callback URL Routing

## Problem

GoTrue constructs the OAuth callback URL as `API_EXTERNAL_URL + "/callback"`, which gives `https://readest.grigri.cloud/callback`. However:

1. Readest's frontend expects the callback at `/auth/callback`
2. GoTrue's actual callback endpoint is at `/auth/v1/callback`

When Kanidm redirects to `/callback`, the request goes to the Readest frontend (404) instead of GoTrue.

## Root Cause

GoTrue's callback URL construction is hardcoded in `internal/api/external.go`:

```go
redirectURL := strings.TrimRight(externalURL, "/") + "/callback"
```

There's no configuration option to override this path. The `API_EXTERNAL_URL` environment variable is used for all URLs, not just the callback.

## Attempted Solutions

### Change API_EXTERNAL_URL

Tried setting `API_EXTERNAL_URL` to `https://readest.grigri.cloud/auth/v1` - **breaks other URLs** like the site URL and redirect URLs.

### Find GoTrue Config

Searched GoTrue configuration for callback path override - **doesn't exist**.

### Update Kanidm Client

Updated the Kanidm OAuth2 client to accept `/callback` as a valid redirect URL - **works but doesn't solve routing**.

## Workaround

Use nginx to redirect `/callback` to `/auth/v1/callback`:

```yaml
ingress:
  external-client:
    annotations:
      nginx.ingress.kubernetes.io/server-snippet: |
        location = /callback {
          return 301 /auth/v1/callback$is_args$args;
        }
```

This redirect is added to the client ingress (which serves the Readest frontend) to intercept the callback before it reaches the frontend.

## How It Works

1. User authenticates with Kanidm
2. Kanidm redirects to `https://readest.grigri.cloud/callback?code=...&state=...`
3. Nginx intercepts the request and returns a 301 redirect to `/auth/v1/callback`
4. The browser follows the redirect to `https://readest.grigri.cloud/auth/v1/callback?code=...&state=...`
5. This request matches the API ingress path `/auth/v1(/|$)(.*)` and is routed to GoTrue
6. GoTrue processes the OAuth callback and completes the authentication

## Impact

- **Extra redirect hop**: Adds one 301 redirect to the OAuth flow
- **Kanidm client config**: Must accept both `/callback` and `/auth/v1/callback` as valid redirect URLs
- **Nginx configuration**: Requires `server-snippet` on the client ingress

## Verification

Check that the redirect is working:

```bash
curl -I https://readest.grigri.cloud/callback
```

Expected output:

```
HTTP/2 301
location: https://readest.grigri.cloud/auth/v1/callback
```

Check the nginx configuration:

```bash
kubectl exec -n ingress-nginx deployment/ingress-nginx-controller -- \
  cat /etc/nginx/nginx.conf | grep -A 5 "location = /callback"
```

## Alternative: Update Kanidm Client

If you want to avoid the nginx redirect, you can update the Kanidm OAuth2 client to only accept `/auth/v1/callback`:

```yaml
apiVersion: kaniop.rs/v1beta1
kind: KanidmOAuth2Client
metadata:
  name: readest
spec:
  redirectUrl:
    - https://readest.grigri.cloud/auth/v1/callback
```

However, this doesn't solve the problem because GoTrue still sends Kanidm to `/callback`. You would need to modify GoTrue's callback URL construction, which requires a custom build.

## Future Improvements

If GoTrue adds support for custom callback paths:
- Remove the nginx redirect
- Configure the callback path directly in GoTrue

If Readest adds a `/callback` route that proxies to GoTrue:
- Remove the nginx redirect
- Let Readest handle the callback routing

## References

- GoTrue source: `internal/api/external.go`
- Readest ingress: `apps/readest/values.yaml`
