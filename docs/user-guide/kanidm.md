# Kanidm

## LDAP connection

For login with LDAP accounts your user has to have enabled the POSIX attributes and need to set a
Unix password.

## Modify ACP

Disallow displayname self modification:

```bash
cat << EOF> /tmp/modify.json
[
    { "removed": ["acp_modify_removedattr", "displayname"] },
    { "removed": ["acp_modify_presentattr", "displayname"] }
]
EOF
kanidm raw modify '{"eq": ["name", "idm_self_acp_write"]}'  /tmp/modify.json
kanidm raw search '{"eq": ["name", "idm_self_acp_write"]}'
```

## Create user

```bash
kanidm person create demo-user "demo-user" -D idm_admin
kanidm person update demo-user --mail "demo-user@example.com" -D idm_admin
kanidm person credential create-reset-token demo-user -D idm_admin

kanidm group list -D idm_admin | rg name | rg users
kanidm group add-members ${GROUP_NAME} demo-user -D idm_admin
```

## Add app to SSO (GitOps with Kaniop)

Preferred method. The [Kaniop operator](https://github.com/kaniop/kaniop) manages OAuth2 clients
and groups declaratively via CRDs. ArgoCD syncs the resources and the operator creates the client
in Kanidm plus a Kubernetes secret with the credentials.

### 1. Create the group

`apps/<name>/templates/kanidm-group.yaml`:

```yaml
---
apiVersion: kaniop.rs/v1beta1
kind: KanidmGroup
metadata:
  name: <app>-users
spec:
  kanidmRef:
    name: kanidm
    namespace: kanidm
```

Optionally add an `<app>-admins` group if the app supports admin scope maps.

### 2. Create the OAuth2 client

`apps/<name>/templates/kanidm-oauth2-client.yaml`:

```yaml
---
apiVersion: kaniop.rs/v1beta1
kind: KanidmOAuth2Client
metadata:
  name: <app>
spec:
  kanidmRef:
    name: kanidm
    namespace: kanidm

  displayname: <app>

  origin: https://<app>.grigri.cloud/

  redirectUrl:
    - https://<app>.grigri.cloud/<callback-path>

  scopeMap:
    - group: <app>-users
      scopes:
        - openid
        - profile
        - email

  # Optional: admin scope map
  # supScopeMap:
  #   - group: <app>-admins
  #     scopes:
  #       - admin

  preferShortUsername: true
  strictRedirectUrl: true
```

Common optional flags:

| Flag | When to use |
|------|-------------|
| `allowInsecureClientDisablePkce: true` | App doesn't support PKCE |
| `public: true` | Public client (no secret, e.g. SPA/native) |
| `allowLocalhostRedirect: true` | CLI tools that redirect to localhost |
| `jwtLegacyCryptoEnable: true` | App needs legacy JWT encryption |

### 3. Consume the secret

The operator creates a secret named `<app>-kanidm-oauth2-credentials` with keys `CLIENT_ID` and
`CLIENT_SECRET`. Reference it in your chart's env vars:

```yaml
env:
  - name: OIDC_CLIENT_ID
    valueFrom:
      secretKeyRef:
        name: <app>-kanidm-oauth2-credentials
        key: CLIENT_ID
  - name: OIDC_CLIENT_SECRET
    valueFrom:
      secretKeyRef:
        name: <app>-kanidm-oauth2-credentials
        key: CLIENT_SECRET
```

The OIDC issuer URL follows this pattern:

```
https://idm.grigri.cloud/oauth2/openid/<app>
```

### 4. Add users to the group

Users must be added manually by an admin after the group is created:

```bash
kanidm group add-members <app>-users ${USER} -D idm_admin
```

### 5. Verify

After ArgoCD syncs, check the client is ready:

```bash
kubectl --context=grigri get kanidmoauth2client <app> -n <namespace>
kubectl --context=grigri get secret <app>-kanidm-oauth2-credentials -n <namespace>
```

The status should show `ready: true` and all conditions `True`.

---

### Manual CLI method (legacy)

Example with Grafana:

```bash
kanidm system oauth2 create grafana grafana  https://grafana.grigri.cloud/login/generic_oauth -D admin
kanidm group create grafana-users -D admin
kanidm group add-members grafana-users ${USER} -D admin
kanidm system oauth2 update-scope-map grafana grafana-users openid profile email -D admin
kanidm system oauth2 show-basic-secret grafana -D admin
kanidm group create grafana-admins -D admin
kanidm group add-members grafana-admins ${USER} -D admin
kanidm system oauth2 update-sup-scope-map grafana grafana-admins admin -D admin
kanidm system oauth2 prefer-short-username grafana -D admin
kanidm system oauth2 set-landing-url grafana https://grafana.grigri.cloud/login/generic_oauth
```

If PKCE needs to be disabled:

```bash
kanidm system oauth2 warning-insecure-client-disable-pkce ${CLIENT}
```

## Use groups in SSO

To pass groups in JWT you need to ask for `openid groups` scopes.

After next login, you will receive groups `uuid` and groups `spn` in the token:

```json
    ...
  "scopes": [
    "email",
    "groups",
    "openid",
    "profile"
  ],
  "groups": [
    "idm_all_persons@idm.grigri.cloud",
    "00000000-0000-0000-0000-000000000035",
    "idm_all_accounts@idm.grigri.cloud",
    "00000000-0000-0000-0000-000000000036",
    "XXXXXX@idm.grigri.cloud",
    "XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX",
    ...
  ]
}
```

**Note**: When groups scope is activated your header size will be above 4k so you will need to add
this annotation to your ingress:

```yaml
nginx.ingress.kubernetes.io/proxy-buffer-size: "16k"
```

## Upgrade

Check if upgrade is possible:

```bash
kanidmd domain upgrade-check
```

## Disable anonymous access

```bash
kanidm service-account validity expire-at anonymous '2024-11-14T00:00:00+01:00'
```

## Increase expiration time

```bash
kanidm group account-policy auth-expiry idm_all_accounts 2592000
```

## Service account permissions to validate credentials

```bash
kanidm group add-members idm_people_pii_read
```
