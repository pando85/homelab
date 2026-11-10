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

**Client identification is out of band.** Kanidm exposes no `registration_endpoint` and no
client-id metadata document (CIMD) support: the authorization-server metadata document at
`<issuer>/.well-known/oauth-authorization-server` omits both fields. Clients are pre-registered
with `KanidmOAuth2Client` and the `client_id` must be supplied to the client out of band. When
`preferShortUsername: true` is set, the `client_id` is the short name (the `metadata.name` of the
CR). A native MCP client that falls through CIMD and dynamic client registration to nothing fails
expecting a `client_id`. Full diagnosis and the reference implementation for in-cluster agents:
[`docs/troubleshooting/kanidm-no-dcr-mcp-client-id.md`](../troubleshooting/kanidm-no-dcr-mcp-client-id.md).

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

### Group membership rules

**Critical constraint**: Person account membership must never be encoded or reconciled through CRDs or Git.

- **Group creation**: Kanidm groups may be created declaratively through `KanidmGroup` CRDs
- **Person membership**: Must be added manually via `kanidm group add-members`. Never encode person accounts in CRD specs or reconcile through Git
- **Service-account-only groups**: If a group consists ONLY of service account users (no person accounts), those memberships may be managed via CRD
- **Mixed groups** (persons + service accounts): Must not use authoritative CRD member reconciliation that could remove manual person memberships. Before managing any service membership in a mixed group, determine whether the operator has additive or preserve semantics. Do not invent support — verify operator behavior first
- **Public repo hygiene**: Do not commit person names, account privilege mappings, or membership lists to public documentation
- **Deleting a `KanidmGroup` CR deletes the group in Kanidm together with its manual memberships** (Kaniop finalizer `kanidmgroups.kaniop.rs/finalizer`). Never move a group CR between apps or charts: the webhook rejects a second CR for the same group, so a move is delete-then-create and the members are lost. Keep the CR in the chart that owns the group, and re-add the members by hand after any accidental delete
- **A `KanidmGroup` CR rendered WITHOUT a `members` field does not clobber manual memberships.**
  Measured 2026-11-10 on the MCP gateway: the chart renders per-route human groups with no `members`
  field, a Kaniop reconcile ran after the owner added people by hand, and the manual membership was
  left intact. The CRD schema says `members` is authoritative *when present*; omitting it is the safe
  way to create a group whose membership is managed out-of-band

**Rationale**: CRD reconciliation may remove members not in the spec. Person accounts require manual lifecycle management (hiring, departure, role changes) that cannot be safely automated through GitOps.

### 5. Verify

After ArgoCD syncs, check the client is ready:

```bash
kubectl --context=grigri get kanidmoauth2client <app> -n <namespace>
kubectl --context=grigri get secret <app>-kanidm-oauth2-credentials -n <namespace>
```

The status should show `ready: true` and all conditions `True`.

---

### Manual CLI method (legacy)

Example with Grafana. Note that these examples use `-D admin`, which was the historical bind account;
the correct bind account for group management today is `idm_admin` (its credentials are in the
owner's password store). The modern Kaniop section above uses `idm_admin` throughout.

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

### kanidm CLI on a workstation

Measured 2026-11-10 on the operator's workstation (client 1.12.0 against server 1.11.2):

- **There is no `~/.config/kanidm/config`.** The only config file present is the packaged example
  `/etc/kanidm/config`, whose `uri` is the placeholder `https://idm.example.com`. Every `kanidm`
  command must therefore pass `-H https://idm.grigri.cloud` explicitly, or the operator must create
  a real instance config at `~/.config/kanidm/config` with the correct `uri`.
- **Client/server version mismatch warning is benign.** The CLI prints "Mismatched client and server
  version" on every call; group read/write operations succeed regardless.
- **Non-interactive login.** `kanidm login` prompts on the TTY and fails with
  `IO(NotConnected, "not a terminal")` in a non-interactive shell. The `KANIDM_PASSWORD` environment
  variable is honoured instead (it backs the `--password` option), which is how an agent or script
  can log in without a TTY. Never put the password in argv or in a file that is not `0600` and
  shredded afterwards.
- **Bind account.** For group management, bind as `idm_admin`, not `admin`. The `admin` account
  still exists but is the historical bootstrap identity; `idm_admin` is the operator account the
  Kaniop operator and the rest of the automation expect.
- **Logout after a session.** `kanidm logout -D idm_admin -H https://idm.grigri.cloud` revokes the
  cached token in `~/.cache/kanidm_cli`. Do it after a session so no long-lived ReadWrite token
  sits on disk.

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

**Note**: Do not request the `groups` scope for a login that goes through an Envoy Gateway
`SecurityPolicy`: the ID token then carries every group of the person and its cookie exceeds the
4096 B browser limit ([ID token cookie size](../troubleshooting/envoy-oidc-id-token-cookie-size.md)).
Use a per-app claim map instead
([Authentication](../conventions/gateway-api-routes.md#authentication)).

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
