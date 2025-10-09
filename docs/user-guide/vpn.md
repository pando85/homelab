# VPN

Add a new user to the VPN group to allow them to connect to the VPN.

```bash
export USER=
kanidm group add-members vpn-users ${USER}
kanidm person posix set ${USER}
```

You can then download the VPN configuration file from the web interface at
`https://pfsense.grigri/vpn_openvpn_export.php`.
