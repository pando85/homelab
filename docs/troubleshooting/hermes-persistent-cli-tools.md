# Persistent CLI Tools in Hermes

## Problem

Tools copied to `/usr/bin`, `/usr/local/bin`, or another container-image directory disappear when
a Hermes pod is recreated. Tool configuration under `/root` disappears for the same reason.

Hermes mounts its persistent volume at `/opt/data`. The image already includes
`/opt/data/.local/bin` in `PATH`, so install standalone tools there and keep their configuration on
the same volume.

This runbook uses the Forgejo CLI (`fj`) as the concrete example.

## Persistent Layout

| Path | Purpose |
|------|---------|
| `/opt/data/.local/bin/fj.bin` | Official `fj` executable |
| `/opt/data/.local/bin/fj` | Wrapper that selects persistent XDG storage |
| `/opt/data/.local/share/forgejo-cli/keys.json` | `fj` authentication data |

The wrapper is necessary because `fj` otherwise stores authentication under
`$HOME/.local/share/forgejo-cli`. The container uses `HOME=/root`, which is not PVC-backed.

## Install `fj`

Set the target instance and pin the release. Do not use an unversioned or latest download URL.

```bash
namespace=hermes-N
pod=hermes-N-0
version=0.6.0
archive=forgejo-cli-x86_64-linux.tar.gz
url="https://codeberg.org/forgejo-contrib/forgejo-cli/releases/download/v${version}/${archive}"

curl --fail --location "$url" --output "/tmp/$archive"

# Upstream did not publish a checksum for this release. The archive verified during this install
# had SHA-256 ea559da5449b6dd7e0dd9f7ea51c906b575696f782edd606b52adcf773602742.
printf '%s  %s\n' \
  'ea559da5449b6dd7e0dd9f7ea51c906b575696f782edd606b52adcf773602742' \
  "/tmp/$archive" | sha256sum --check

mkdir -p /tmp/forgejo-cli
tar --extract --gzip --file "/tmp/$archive" --directory /tmp/forgejo-cli

kubectl --context=grigri exec -n "$namespace" "$pod" -- \
  mkdir -p /opt/data/.local/bin /opt/data/.local/share
kubectl --context=grigri cp /tmp/forgejo-cli/fj \
  "$namespace/$pod:/opt/data/.local/bin/fj.bin"
```

Create the persistent-data wrapper without placing a token in shell history:

```bash
kubectl --context=grigri exec -i -n "$namespace" "$pod" -- sh -c \
  'umask 022; cat > /opt/data/.local/bin/fj' <<'EOF'
#!/bin/sh
XDG_DATA_HOME=/opt/data/.local/share exec /opt/data/.local/bin/fj.bin "$@"
EOF

kubectl --context=grigri exec -n "$namespace" "$pod" -- sh -c \
  'chmod 0755 /opt/data/.local/bin/fj /opt/data/.local/bin/fj.bin && \
   chown hermes:hermes /opt/data/.local/bin/fj /opt/data/.local/bin/fj.bin && \
   fj version'
```

Use the official release archive rather than copying the local workstation package. A
distribution package may depend on shared-library versions not present in the Hermes Debian image.

## Create and Configure a Forgejo Token

Create the token from an account whose access is limited to the intended organization. For Hermes
3, that organization is `multivara`. Forgejo tokens inherit the account's permissions, and the
**All repositories** resource selection is account-wide rather than restricted to one organization.
Do not use a broadly privileged personal or administrator account if strict organization isolation
is required.

In `git.grigri.cloud`, open **Settings > Applications > New access token** and select:

- Resource access: **All repositories**
- `write:organization`
- `write:repository`
- `write:issue` (also covers pull requests)
- `read:user`

Do not grant `admin`, `package`, `notification`, or ActivityPub access unless the agent has a
specific requirement. A token cannot exceed the Forgejo account's existing organization and
repository permissions.

Use `write:user` instead of `read:user` only when the agent must modify user data.

Enter the token through a hidden prompt and pipe it directly to the pod. Do not put it in a command
argument, Git, a temporary file, or shell history.

```bash
read -rsp 'Forgejo token: ' forgejo_token
printf '\n'
printf '%s' "$forgejo_token" | kubectl --context=grigri exec -i \
  -n "$namespace" "$pod" -- fj auth add-token -H git.grigri.cloud
unset forgejo_token

kubectl --context=grigri exec -n "$namespace" "$pod" -- sh -c \
  'chown -R hermes:hermes /opt/data/.local/share/forgejo-cli && \
   chmod 0700 /opt/data/.local/share/forgejo-cli && \
   chmod 0600 /opt/data/.local/share/forgejo-cli/keys.json'
```

`keys.json` contains a bearer token in plaintext. It is protected by filesystem permissions and
the Kata-isolated, PVC-backed instance boundary, but it is also present in PVC snapshots and
backups. Use a dedicated, least-privilege token and rotate it if the instance or a backup is
compromised.

## Verify

```bash
kubectl --context=grigri exec -n "$namespace" "$pod" -- fj auth list
kubectl --context=grigri exec -n "$namespace" "$pod" -- \
  fj -H git.grigri.cloud org view multivara
kubectl --context=grigri exec -n "$namespace" "$pod" -- \
  fj -H git.grigri.cloud org repo list multivara

# Confirm both the executable and authentication data are on the PVC.
kubectl --context=grigri exec -n "$namespace" "$pod" -- sh -c \
  'test -x /opt/data/.local/bin/fj.bin && \
   test -f /opt/data/.local/share/forgejo-cli/keys.json'
```

After the next normal pod recreation, rerun the verification commands. Do not manually restart a
healthy agent solely to test persistence.

## Upgrade or Revoke

To upgrade, download and verify a new pinned release, then replace only
`/opt/data/.local/bin/fj.bin`. Keep the wrapper and authentication directory unchanged.

To revoke access, delete the token in Forgejo first, then remove the local authentication entry:

```bash
kubectl --context=grigri exec -n "$namespace" "$pod" -- \
  fj auth logout git.grigri.cloud
```
