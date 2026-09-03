# GoTrue SSRF Protection Blocks Private IPs

## Problem

GoTrue's custom OAuth provider API (`/admin/custom-providers`) validates URLs and blocks private IPs (RFC 1918) to prevent SSRF attacks. When trying to register a custom OAuth provider pointing to an internal service (e.g., `idm.grigri.cloud` resolving to `192.168.193.4`), the API returns:

```
400: URL cannot resolve to private network addresses
```

## Root Cause

GoTrue's `ValidateOAuthURL` function in `internal/utilities/url_validator.go` performs DNS resolution and checks each IP address against:
- Loopback addresses (127.0.0.0/8, ::1)
- Private network addresses (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
- Link-local addresses (169.254.0.0/16, fe80::/10)
- Cloud metadata endpoints (169.254.169.254)
- Multicast addresses

There is no configuration option to whitelist specific hosts or disable this validation.

## Attempted Solutions

### Environment Variable

Tried `GOTRUE_CUSTOM_OAUTH_PRIVATE_HOSTS` environment variable - **doesn't exist** in GoTrue v2.196.0.

### Provider Type

Tried both `oidc` and `oauth2` provider types - both perform the same URL validation.

### External DNS

Tried using the public IP address instead of internal DNS - the public IP may not be routable from within the cluster, and this defeats the purpose of internal service discovery.

## Workaround

Insert the custom provider directly into the `auth.custom_oauth_providers` table via SQL, bypassing the API validation:

```sql
INSERT INTO auth.custom_oauth_providers (
  provider_type,
  identifier,
  name,
  client_id,
  client_secret,
  authorization_url,
  token_url,
  userinfo_url,
  scopes,
  pkce_enabled,
  enabled
) VALUES (
  'oauth2',
  'custom:kanidm',
  'Kanidm',
  '<client_id>',
  '<client_secret>',
  'https://idm.grigri.cloud/ui/oauth2',
  'https://idm.grigri.cloud/oauth2/token',
  'https://idm.grigri.cloud/oauth2/openid/readest/userinfo',
  ARRAY['openid', 'profile', 'email'],
  true,
  true
);
```

## Impact

- The custom provider is **not managed by GitOps**
- Must be manually inserted if the database is recreated
- Cannot use GoTrue's admin API to update or delete the provider
- No validation of the URLs (could point to invalid endpoints)

## Verification

Verify the provider was inserted correctly:

```sql
SELECT identifier, name, provider_type, enabled 
FROM auth.custom_oauth_providers;
```

Or via GoTrue admin API:

```bash
curl -H "Authorization: Bearer <service_role_key>" \
  http://localhost:9999/admin/custom-providers
```

## Future Improvements

If GoTrue adds support for private hosts (e.g., via environment variable or config), we can:
- Use the GoTrue API to register the provider
- Manage the provider configuration via GitOps
- Get URL validation and error handling

## References

- GoTrue source: `internal/utilities/url_validator.go`
- GoTrue issue: https://github.com/supabase/auth/issues/1234 (example)
