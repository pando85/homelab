# Playwright MCP

Playwright MCP server with a co-located headless Chromium, reachable only via
`kubectl port-forward` (Stage 1).

## Stage 1 (this chart)

One pod, MCP server + browser together. No HTTPRoute, no Ingress, no Envoy
SecurityPolicy, no Kanidm OAuth client or group. The upstream image states
plainly: **"Playwright MCP is not a security boundary."** Exposing an
unauthenticated browser driver to the LAN is not acceptable, so it stays
unexposed until the auth question is settled.

### Access

```bash
kubectl --context=grigri -n playwright-mcp port-forward svc/playwright-mcp 8931:8931
```

OpenCode config (`~/.config/opencode/opencode.json`):

```json
{
  "mcp": {
    "playwright": {
      "type": "remote",
      "url": "http://127.0.0.1:8931/mcp"
    }
  }
}
```

No OAuth: the path is loopback-only.

### Threat model

The browser is driven by an agent and visits arbitrary URLs, so it is treated
as untrusted. The CiliumNetworkPolicy denies cluster and LAN destinations to
prevent it becoming a pivot (SSRF into the cluster, the LAN, or pfSense). The
pod holds no cluster identity (`automountServiceAccountToken: false`).

Egress is internet-only via `toCIDRSet` with `cidr: 0.0.0.0/0` and an `except`
list covering the cluster's pod CIDR (`10.0.0.0/8`), service CIDR
(`10.43.0.0/16`), RFC1918 (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`),
link-local (`169.254.0.0/16`), and CGNAT (`100.64.0.0/10`). DNS to the
`node-local-dns` and `kube-dns` pods in `kube-system` is allowed separately.

### Stage 2 (pending)

Authenticated exposure via the Envoy Gateway requires resolving two unknowns:

1. Does a Kanidm `claimMap` value land in the ACCESS token or only the ID
   token?
2. Can an Envoy Gateway SecurityPolicy jwt provider extract from the
   `Authorization` header and authorize on that claim?

Neither is implemented here. The repo's Envoy OIDC pattern (apps/http-echo
precedent: `apps/http-echo/templates/security-policy.yaml`) validates an ID
token extracted from the `oidc-id-token` COOKIE, which is browser-shaped;
bearer-mode auth for an MCP client is unverified.

### Ephemeral browser profile

Stage 1 uses an ephemeral browser profile (no PVC). The profile stays in
memory for the life of the process and is wiped when the container restarts.
A persisted profile would be a cookie store that Velero replicates to S3, so
it needs an explicit owner decision before being enabled.

## Image facts

Verified against `mcr.microsoft.com/playwright/mcp:v0.0.85`:

- Multi-arch index (amd64 + arm64)
- ENTRYPOINT is `/usr/bin/tini -s -- node /app/cli.js --headless --browser
  chromium --no-sandbox` - the image already runs the MCP server under tini
  with the right browser flags, so the container only needs to append
  `--port 8931 --host 0.0.0.0`. Keeping the image's own ENTRYPOINT (rather
  than overriding it with `command:`) preserves tini as PID 1, which matters
  for reaping Chromium's child processes.
- USER node (uid 1000, gid 1000, verified from image history)
- WORKDIR /home/node
- PLAYWRIGHT_BROWSERS_PATH=/ms-playwright (pre-installed browsers)
- The image serves the MCP Streamable HTTP endpoint at `/mcp`

## Verification

```bash
# Render and lint
helm dependency build apps/playwright-mcp/
helm template --include-crds --namespace playwright-mcp playwright-mcp apps/playwright-mcp/
helm lint apps/playwright-mcp/

# Run the chart tests
python3 test/playwright-mcp/chart-checks.py

# After ArgoCD syncs, run the live acceptance
bash test/playwright-mcp/live-acceptance.sh
```

The live-acceptance script drives a real MCP session over HTTP on
`127.0.0.1:8931` from inside the pod, navigates to `https://example.com`, and
asserts the snapshot contains `Example Domain`. It also verifies the egress
policy: internet HTTPS succeeds, cluster Service addresses time out, and LAN
addresses time out.

## Rollback

- Disable the CNP: set `networkIsolation: false` in `values.yaml`
- Disable `readOnlyRootFilesystem`: set to `false` in `values.yaml` if
  Chromium fails to start with a read-only path error (the live-acceptance
  script will surface this immediately)
