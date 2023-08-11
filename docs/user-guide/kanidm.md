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
