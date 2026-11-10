# Playwright MCP

Playwright MCP server with a co-located headless Chromium, exposed through
the MCP gateway with Kanidm OIDC (Stage 2).

## Stage 2 (current)

Authenticated exposure via the MCP gateway at
`https://mcp.grigri.cloud/playwright/mcp`. The gateway validates Kanidm
OIDC tokens with the `mcp_playwright` scope and forwards to the in-cluster
pod. No tunnel needed.

### Access

OpenCode config (`~/.config/opencode/opencode.json`):

```json
{
  "mcp": {
    "playwright": {
      "type": "remote",
      "url": "https://mcp.grigri.cloud/playwright/mcp",
      "client_id": "mcp-gateway",
      "scope": "mcp_playwright",
      "callback_port": 15000,
      "redirect_uri": "http://127.0.0.1:15000/callback"
    }
  }
}
```

Kanidm has no dynamic client registration and no client_id metadata
documents, so the pre-registered `client_id: mcp-gateway` must be given
explicitly. See `docs/troubleshooting/kanidm-no-dcr-mcp-client-id.md`.

Membership in the `mcp-playwright-users` Kanidm group is required. The
owner adds members manually:

```bash
kanidm group add-members mcp-playwright-users <account> \
  -D idm_admin -H https://idm.grigri.cloud
```

### Session continuity

The route uses `statefulMode: stateful` because Playwright MCP is stateful
(browser context lives in an `Mcp-Session-Id`). The Phase A probe
(`test/playwright-mcp/session-propagation-probe.py`) measured that
`stateless` mode drops the backend-issued session id, so
`browser_navigate` followed by `browser_snapshot` would hit a fresh
browser. Stateful mode has the gateway issue its own session id and
translate it back to the backend's on each request, preserving continuity.

This is the only stateful route in the gateway, so it was verified rather
than assumed safe. The other routes chose `stateless` to stop a session id
becoming "a second, unauthenticated way to address a backend session"
(`apps/mcp-gateway/values.yaml:405-407`). The probe's negative tests confirm
that does not happen here: a valid session id presented with no
`Authorization`, with a garbage token, or with a well-formed token carrying
another route's scope is refused in all three cases, a forged session id is
not accepted, and none of the refused requests reach the backend.
Authentication and the CEL scope gate re-apply on every request, not just
`initialize`. Translation also means the client never learns the backend's
real session id. Stateful mode is safe on this route specifically because it
has no `backendAuth`, so there is no backend credential to cache per session.

### Threat model

The upstream image states plainly: **"Playwright MCP is not a security boundary."** The CiliumNetworkPolicy admits only the gateway pods on port
8931 (no `world`, no `cluster`, no `host`). The browser is driven by an
agent and visits arbitrary URLs, so it is treated as untrusted. The CNP
denies cluster and LAN destinations to prevent it becoming a pivot (SSRF
into the cluster, the LAN, or pfSense). The pod holds no cluster identity
(`automountServiceAccountToken: false`).

Egress is internet-only via `toCIDRSet` with `cidr: 0.0.0.0/0` and an
`except` list covering the cluster's pod CIDR (`10.0.0.0/8`), service CIDR
(`10.43.0.0/16`), RFC1918 (`10.0.0.0/8`, `172.16.0.0/12`,
`192.168.0.0/16`), link-local (`169.254.0.0/16`), and CGNAT
(`100.64.0.0/10`). DNS to the `node-local-dns` and `kube-dns` pods in
`kube-system` is allowed separately.

### Ephemeral browser profile

Stage 2 uses an ephemeral browser profile (no PVC), set by
`PLAYWRIGHT_MCP_ISOLATED=true`. Upstream defines `--isolated` as "keep the
browser profile in memory, do not save it to disk", so no cookie store ever
lands on a volume. A persisted profile would be a cookie store that Velero
replicates to S3, so it needs an explicit owner decision before being enabled.

Isolated mode and `--user-data-dir` are mutually exclusive: setting both makes
v0.0.85 exit 1 at startup with "Browser userDataDir is not supported in
isolated mode". `test/playwright-mcp/chart-checks.py` asserts the absence of
both the flag and its `PLAYWRIGHT_MCP_USER_DATA_DIR` env form.

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

# Run the session propagation probe (Phase A)
python3 test/playwright-mcp/session-propagation-probe.py

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
