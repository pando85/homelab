# pfSense alias change keeps old states

## Problem

After repointing the pfSense alias `k8s_external_ingress` (the target of the WAN port forwards for
80, 443 and 29518) from one gateway VIP to another, some traffic still reached the old VIP. The new
target was correct and new connections used it, but the old ingress controller kept receiving
requests.

## Root Cause

Two separate effects:

1. **pf keeps the redirect target of an existing state.** An alias edit changes the rules, not the
   states already created from them. Long-lived connections keep going to the old target until they
   close or the state is killed. Seen in one switch (16 states): one HTTP/2 connection from a LAN
   workstation, which carried requests for several hostnames (one h2 connection serves several
   hostnames), an SSH session on 29518, a phone webhook client, and an outside
   client on port 80.
2. **A host override can be missed.** The unbound host overrides (Services > DNS Resolver > Host
   Overrides) hold the VIP separately from the alias. In one switch one of the three overrides
   (`app.staging.forkline.dev` and its aliases) was left on the old IP on the first pass. It was
   only found because a post-check counted the overrides still on the old IP.

## How to Diagnose

Read-only, over `ssh pfsense.grigri`:

```sh
pfctl -t k8s_external_ingress -T show                  # alias content
pfctl -sn | grep -c "rdr.*k8s_external_ingress"        # rdr rules using the alias (21)
pfctl -ss | grep -c "192.168.193.4:"                   # states still on the old VIP
grep -E '^local-data: "[^ ]+ A 192.168.193.' /var/unbound/host_entries.conf   # overrides: count per IP
```

Also check that the old controller stops receiving new requests (its access log). Always count
overrides per IP; do not eyeball the list.

## Fix / Workaround

pfSense changes are owner-only. After the alias edit, the owner opens Diagnostics > States, filters on
the old IP and kills the matching states. Then verify the state count above is `0` and the old
controller logs no new requests. For a missed override, set its host IP to the new VIP (aliases
follow the host) and re-count.

Do the state kill as part of the switch plan, not as an afterthought, and expect clients with
persistent connections (SSH, h2, webhooks) to be the ones that linger.

See [Ingress](../network/ingress.md) for the alias and override layout.
