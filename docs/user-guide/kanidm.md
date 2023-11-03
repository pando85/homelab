# Kanidm

## LDAP connection

For login with LDAP accounts your user has to has enabled the POSIX attributes and need to set a Unix password.

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
# LDAP access (jellyfin)
kanidm person posix set demo-user
kanidm person update demo-user --mail "demo-user@example.com"
kanidm person credential create-reset-token demo-user

kanidm group list | rg name | rg users
kanidm group add-members ${GROUP_NAME} demo-user
```

## Add app to SSO

Example with Grafana:
```bash
kanidm system oauth2 create grafana grafana  https://grafana.grigri.cloud/login/generic_oauth -D admin
kanidm group create grafana-users -D admin
kanidm group add-members grafana-users XXXXX -D admin
kanidm system oauth2 update-scope-map grafana grafana-users openid profile email -D admin
kanidm system oauth2 show-basic-secret grafana -D admin
kanidm group create grafana-admins -D admin
kanidm group add-members grafana-admins XXXXX -D admin
kanidm system oauth2 update-sup-scope-map grafana grafana-admins admin -D admin
kanidm system oauth2 prefer-short-username grafana -D admin
kanidm system oauth2 set-landing-url grafana https://grafana.grigri.cloud/login/generic_oauth
```
