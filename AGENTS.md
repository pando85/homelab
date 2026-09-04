# AGENTS.md

Concise repository guidance for agentic coding agents. Keep this file short: add detailed runbooks
under `docs/`, and only keep high-signal reminders here.

## Repository Overview

Self-hosted Kubernetes (K3s) homelab managed via GitOps. Core principles: declarative
infrastructure, repeatable automation, small blast radius, explicit version pinning, minimal drift.

**Tech Stack:** K3s, ArgoCD, Helm (template-only), Ansible (bootstrap), External Secrets + Vault,
cert-manager, ingress-nginx, Renovate, ZFS, Cilium (BGP).

**Cluster Access:** This is a GitOps repository. Use `kubectl --context=grigri` to see what is
running in the cluster. Metrics are available at https://prometheus.internal.grigri.cloud

## Repository Map

```
apps/      # Application Helm charts, one release per folder
system/    # Cluster-wide components: ingress, cert-manager, monitoring, identity
platform/  # Supporting operators/services: Vault, external-secrets, git, reloader
bootstrap/ # ArgoCD bootstrap layer
metal/     # Ansible inventory, playbooks, roles for K3s node provisioning
scripts/   # Utility scripts, including deploy-dir.rh for Helm rendering
test/      # Local k3d test helpers
docs/      # MkDocs documentation and runbooks
```

## Build/Lint/Test Commands

```bash
# Install hooks
make git-hooks

# Run all checks
pre-commit run --all-files

# Build chart dependencies
helm dependency build apps/<name>/

# Template chart
helm template --include-crds --namespace <namespace> <release-name> apps/<name>/

# Validate chart
helm lint apps/<name>/
```

## Workflows

**When deploying a new application or adding functionality to an existing app:**
1. Read `docs/conventions/deploying-new-apps.md` — decision trees, patterns, and checklist
2. Check existing deployments for reference patterns:
   - Multi-controller apps: `apps/immich/`, `apps/readest/`
   - OIDC integration: `apps/immich/templates/kanidm-oauth2-client.yaml`
   - Zalando Postgres: `apps/immich/templates/postgresql.yaml`, `apps/dawarich/templates/postgresql.yaml`
   - Shared MinIO: `platform/minio/values.yaml`
3. Follow the validation commands above before committing

## Code Style

- **YAML:** 2-space indent, `---` for multi-doc, yamllint via pre-commit (ignores `templates/`)
- **Helm:** Use `app-template` from bjw-s. See `docs/conventions/helm.md` for full patterns
- **Ansible:** `safety` profile. See `docs/conventions/ansible.md`
- **Renovate:** Always add hints above image refs — see `docs/conventions/helm.md#renovate-integration`
- **Docs:** Put durable troubleshooting details in `docs/troubleshooting/`; keep `AGENTS.md` to links
  and one-line reminders. See `docs/conventions/documenting-learnings.md`

## Commit Messages

**Format:** `<scope>: <imperative-description>`

- Scope: directory or service name (`kube-system:`, `vault:`, `monitoring:`)
- Special scopes: `metal` (Ansible), `docs` (documentation), `renovate` (renovate config)

```
kube-system: Add kata-nvidia coordinator operator
vault: Change to svg logo
monitoring: Update Helm release kube-prometheus-stack to v82.10.1
```

## Deployment Restrictions

**CRITICAL: NEVER automatically execute deployment commands.**

- **NEVER run:** `make bootstrap`, `make metal`, `make dev`, `kubectl apply`,
  `helm install/upgrade`, `ansible-playbook`
- **NEVER run:** `kubectl delete`, `kubectl edit`, `kubectl patch` on cluster resources
- **ALWAYS:** Only suggest commands for user to run manually
- **ALL cluster changes MUST go through GitOps:** commit/push to repo, let ArgoCD sync
- **Metal node provisioning:** `cd metal && ANSIBLE_EXTRA_ARGS="-t k3s" make cluster`
  (user must run manually)

**Why:** ArgoCD has `selfHeal: true` with real-time cluster watches. Any direct `kubectl` mutations
will be detected and reverted almost instantly. The git repository is the single source of truth.

## Skill Usage

- Use the `debug` skill only for live cluster investigation with read-only `kubectl` and Grafana
  observability.
- Do not copy runbook steps into agent instructions. Link to the relevant docs page instead.
- Prefer focused, path-specific docs over adding broad always-loaded guidance.

## Security

- Never hardcode secrets, tokens, or private keys
- Use ExternalSecret pointing to Vault for all sensitive data
- Assume public repository hygiene at all times

## Common Pitfalls

- Forgetting `helm dependency build` after updating `Chart.yaml`
- Missing Renovate hints causes images to not auto-update
- Grafana dashboard sidecar only honors `grafana.grafana.com/dashboards.target-directory`, not
  `k8s-sidecar-target-directory` — using the latter silently drops the dashboard at the root
  folder. See `docs/troubleshooting/grafana-sidecar-folder-annotation.md`
- Grafana datasource sidecar writes ConfigMap data keys as filenames — if two ConfigMaps use the
  same data key (e.g., `datasource.yaml`), they collide (last-writer-wins) and cause continuous
  reload churn. Use unique keys per ConfigMap (e.g., `loki-datasource.yaml`,
  `tempo-datasource.yaml`). See `docs/troubleshooting/grafana-datasource-sidecar-collision.md`
- Grafana 13.1.1 strips the `url` field from Loki `derivedFields` during provisioning, even with
  `$$` escaping. Workaround: manually set the URL in the Grafana UI. See
  `docs/troubleshooting/grafana-13-derivedfields-url-stripped.md`
- Introducing CRDs without `--include-crds` in helm template
- Not waiting for webhooks (cert-manager, external-secrets) before applying dependent resources
- Forgetting `Prune=false` on PVCs causes data loss on sync
- Kata-deploy 3.31.0+ requires containerd drop-in directory (`config-v3.toml.d`) — see
  `docs/troubleshooting/kata-containerd-dropin.md`
- `openebs-zfspv` storage class uses `reclaimPolicy: Retain` — deleted PVCs leave released PVs
  that leak ZFS space. Audit periodically: see `docs/troubleshooting/cluster-hygiene.md`
- `openebs-zfspv` with `fstype: zfs` + `fsGroup` causes slow pod startup (recursive chown on
  every mount). `fsGroupChangePolicy: OnRootMismatch` doesn't help — kubelet resets setgid bit.
  If the app manages its own file ownership (runs as volume owner or has init chown), remove
  `fsGroup` entirely. See `docs/troubleshooting/openebs-zfspv-slow-startup-fsgroup.md`
- Apps with Supabase dependencies (auth schema, GoTrue, PostgREST) can use Zalando Postgres +
  init container for bootstrap SQL. Don't deploy separate Supabase Postgres container unless
  the app requires Supabase-specific extensions not in the Spilo image. See
  `docs/deployment/readest.md` and `docs/conventions/deploying-new-apps.md`
- Supabase apps on Zalando Postgres require manual schema setup: create `auth`, `storage`,
  `realtime`, `graphql_public` schemas and enum types (`auth.factor_type`, etc.) before GoTrue
  migrations run. This is a one-time operation per database. See `docs/deployment/readest.md`
- High pod restart counts don't always mean problems — check `Last State.Reason` (exit 255 =
  node reboot, not app crash). See `docs/troubleshooting/cluster-hygiene.md`
- Armbian kernel 6.12 on Odroid HC4 breaks Cilium UDP BPF masquerading — hold kernel at 6.6 LTS
  (`24.11.1`). See `docs/troubleshooting/armbian-kernel-bpf-masquerade.md`
- Cilium BPF datapath can go stale on a node — pod egress breaks while host network works. Can
  manifest as partial breakage (some connections work, others don't) — e.g. Vector buffer filling
  on one sink while another on the same pod is fine. Also triggered by node reboots (BPF link
  orphaning, upstream #46065); enabling NetworkPolicy turns the latent staleness into an active
  probe-failure outage. Fix: delete the Cilium pod.
  See `docs/troubleshooting/cilium-stale-bpf-egress.md`
- Cilium LRP `skipRedirectFromBackend` is broken in v1.19.4 — nodelocaldns with `serviceMatcher`
  needs a corefile-watcher sidecar to forward directly to CoreDNS pod IPs, avoiding redirect loop.
  `addressMatcher` has post-reboot bugs (PR #45522). See
  `docs/troubleshooting/nodelocaldns-cilium-lrp.md`
- ArgoCD has `selfHeal: true` with real-time cluster watches — `kubectl apply` will be detected
  and reverted almost instantly. Always commit/push first, let ArgoCD sync, then verify.
  See `docs/troubleshooting/argocd-gitops-workflow.md`
- ArgoCD repo-server has a probe death-spiral with default chart probes (1s timeout) — the pod
  is killed during slow cold start before the metrics/health port (8084) binds, causing a restart
  loop that never converges. Fix: enable `startupProbe` + raise `timeoutSeconds` to 5. See
  `docs/troubleshooting/argocd-repo-server-probe-death-spiral.md`
- ArgoCD `argo-cd` chart >= 10.0 defaults `networkPolicy.create: true` — keep it `false` because
  it interacts with Cilium's stale-datapath bug on rebooted nodes (kubelet probes get dropped).
  See `docs/troubleshooting/argocd-repo-server-probe-death-spiral.md`
- Unattended-upgrades must blacklist NVIDIA packages — host-level driver upgrades conflict with
  the GPU Operator's containerized driver management, causing `Driver/library version mismatch`.
  Fix: `cd metal && ANSIBLE_EXTRA_ARGS="-t unattended-upgrades" make prepare`
  See `docs/troubleshooting/nvidia-driver-version-mismatch.md`
- Unattended-upgrades can restart k3s during backup windows, causing `PartiallyFailed` Velero backups.
  Use a broad maintenance window (Mon + Wed–Sun 08:00–18:00) instead of blacklisting systemd packages.
  See `docs/troubleshooting/velero-backup-failures.md`
- Zalando Postgres operator rejects hyphenated database names in the `databases` field — create
  them manually with `psql`. See `docs/troubleshooting/radarr-sqlite-to-postgres.md`
- Hermes instance config overwrite: when deploying a new Hermes instance, never copy `config.yaml`,
  `memories/`, or `skills/` from another instance — this overwrites the target's unique identity.
  Only copy `auth.json` if needed. Recovery requires ZFS snapshot rollback.
  See `docs/troubleshooting/hermes-config-overwrite-recovery.md`
- Hermes standalone tools and their configuration must live under the `/opt/data` PVC; installs in
  the container root filesystem or `/root` disappear on pod recreation. For `fj`, use a persistent
  XDG data wrapper. See `docs/troubleshooting/hermes-persistent-cli-tools.md`
- Hermes auth failures (401 errors) after config edits: manual `config.yaml` edits drop required
  fields, `auth.json` may have empty `base_url`, and `state.db` caches stale credentials. Fix:
  copy working config from another instance, ensure `base_url` is set in auth.json, delete
  `state.db`, and **delete the pod** (don't just pkill). See
  `docs/troubleshooting/hermes-authentication-credential-issues.md`
- tc-limiter hostPath mounts need `mountPropagation: HostToContainer` — otherwise Cilium socket
  goes stale after restart and rate limiting silently stops working.
  See `docs/troubleshooting/bandwidth-limiting.md`
- Stump v0.1.5+ migration `m20260519_192218_reading_sessions_v2` can fail mid-way, leaving legacy
  tables. Restore from snapshot and complete migration manually.
  See `docs/troubleshooting/stump-migration-failure.md`
- `home-operations/home-assistant` uses a venv at `/config/.venv` with `--system-site-packages`.
  System packages are read-only; install user packages with `/config/.venv/bin/uv pip install`.
  See `docs/troubleshooting/home-assistant-python-packages.md`
- `home-operations/home-assistant:2026.7.1` ships with aiohttp 3.14.1 (system) which removed
  `decode_text` parameter, breaking WebSocket API. Fix: install `aiohttp==3.14.0` in venv.
  See `docs/troubleshooting/home-assistant-aiohttp-incompatibility.md`
- ESIOS API (`api.esios.ree.es`) returns ZIP archives with `Content-Type: text/html` instead of
  JSON for `/archives/70/download_json`, breaking the `pvpc_updated` integration. Workaround: patch
  `pvpc_data.py` to handle ZIP format. See `docs/troubleshooting/pvpc-updated-esios-api-zip-response.md`
- Negative PVPC prices are normal with high solar generation. AppDaemon climate/DHW control used to
  reject them with a bare `ValueError` (empty `Error getting prices:` log), halting aerotherm
  scheduling. Apps now accept negative prices and use a fallback schedule when price fetching fails.
  See `docs/troubleshooting/appdaemon-pvpc-negative-prices.md`
- qBittorrent with HostPath volumes to HDDs saturates disks at ~240 IOPS, causing latency spikes
  in other workloads (Forgejo, etc.). Fix: run qBittorrent with `ionice -c 3` (idle I/O priority)
  and increase startup probe timeout. See `docs/troubleshooting/qbittorrent-hdd-io-saturation.md`
- vault-operator chart template doesn't support `failureThreshold` in probe config — only
  `timeoutSeconds`, `periodSeconds`, `successThreshold`, `initialDelaySeconds` are rendered.
  See `docs/troubleshooting/vault-operator-probe-timeout.md`
- Kaniop `KanidmBackupSchedule.spec.schedule` is immutable — changing schedule requires
  delete/recreate. Discovery controller runs every 5 min and creates CRs for ALL S3 manifests
  (1000 limit). Retention only deletes CRs, not orphaned S3 data. To clean up: delete schedule,
  delete CRs, clean S3 with `mc rm --recursive --force --versions`, then recreate schedule.
  See `docs/troubleshooting/kaniop-backup-system.md`
- Kanidm restore requires `KanidmRestore` CR with matching `targetRef.uid`, pinned `restoreImage`,
  and safety backup (or break-glass annotations). Restore job permission bug (#1005) causes failures.
  See `docs/troubleshooting/kanidm-restore-procedure.md`
- GoTrue's CORS allow-list omits the `apikey` header that supabase-js sends on every request. Web
  clients are same-origin (no preflight), but cross-origin Tauri/mobile WebViews fail silently at
  preflight and session establishment dies ("go to login"). Fix: `GOTRUE_CORS_ALLOWED_HEADERS: apikey`.
  Android login also needs `readest://auth-callback` in `GOTRUE_URI_ALLOW_LIST` and an nginx
  provider rewrite that preserves `redirect_to`. See `docs/troubleshooting/readest-android-oauth.md`

## Subsystem Docs

- **Cilium networking:** See `docs/conventions/cilium.md` for BGP, TCX, bandwidth limiting details
- **Deploying new apps:** See `docs/conventions/deploying-new-apps.md` for the decision-making
  process, patterns, and checklist when adding a new application
- **Documenting learnings:** See `docs/conventions/documenting-learnings.md` for when/how to write
  troubleshooting docs

## Licensing

GPLv3 (see LICENSE.md). Generated code must be compatible.
