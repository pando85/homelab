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

## Deployment Rules

**Commit first, then apply.** Git must contain the change before it is applied anywhere, so every
action is reviewable, reproducible, and revertible. Never apply an uncommitted or partially edited
tree.

### Agents may run

- Read-only inspection: `kubectl get/describe/logs/top`, Prometheus/Grafana/Loki queries,
  `helm template`, `helm lint`, `helm dependency build`, `pre-commit run`
- **Narrowly scoped `metal/` Ansible** for config that is already committed, using explicit
  `--tags` and `--limit`. Example: `cd metal && ANSIBLE_EXTRA_ARGS="-t zfs-config --limit prusik" make prepare`
- ArgoCD sync of an already-committed and pushed change

Report the command run and its outcome. Prefer the narrowest tag and limit that does the job.

### Never run

- **`kubectl apply/delete/edit/patch`** on cluster resources — ArgoCD `selfHeal: true` with real-time
  watches reverts these almost instantly, so they are futile, not merely risky
- **Node lifecycle / high blast radius:** `make cluster`, `make bootstrap`, `make dev`,
  `make uninstall-k3s`, anything with `-t k3s`. These can take down the sole control-plane node
- **Anything data-destroying:** PVC/PV deletion, `zpool destroy`, `zfs destroy`, pool re-creation,
  disk formatting (`prepare_additional_disks_force_format`)
- Untagged or unlimited `make prepare` / `ansible-playbook` runs — always scope them
- `helm install/upgrade` — let ArgoCD do it

When in doubt, or when a change needs a hard prohibition above, stop and hand the user the exact
command.

**Why:** ArgoCD is the source of truth for cluster state, so direct mutations get reverted. `metal/`
is host-level config outside ArgoCD, which is why it needs an explicit apply step — and why scoping
matters there.

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
- Grafana panels querying node-exporter metrics must collapse the DaemonSet's `pod` label with
  `max by (instance, ...)` — otherwise every pod restart adds another phantom line over long time
  ranges, and `sum(rate(...))` panels silently inflate. Use `max`, not `sum`/`avg`. Also verify each
  metric name actually exists: empty panels look identical to nonexistent metrics. See
  `docs/troubleshooting/grafana-node-exporter-pod-churn-duplicates.md`
- Grafana 13.1.1 strips the `url` field from Loki `derivedFields` during provisioning, even with
  `$$` escaping. Workaround: manually set the URL in the Grafana UI. See
  `docs/troubleshooting/grafana-13-derivedfields-url-stripped.md`
- Grafana MCP server deployment has multiple gotchas: Kaniop `KanidmServiceAccount` requires
  `idm_admin` in the `entryManagedBy` group and `serviceAccountNamespaceSelector` on the Kanidm CR;
  `mcp-grafana` image tags lack `v` prefix, binary is at `/app/mcp-grafana`, uses `-address` flag
  (not `-port`); StatefulSets need manual pod deletion after template changes. See
  `docs/troubleshooting/grafana-mcp-hermes.md`
- Introducing CRDs without `--include-crds` in helm template
- Not waiting for webhooks (cert-manager, external-secrets) before applying dependent resources
- Forgetting `Prune=false` on PVCs causes data loss on sync
- Kata-deploy 3.31.0+ requires containerd drop-in directory (`config-v3.toml.d`) — see
  `docs/troubleshooting/kata-containerd-dropin.md`
- Galaxy roles under `metal/roles/` are gitignored — never patch them, wrap them in a repo role and
  override via `include_role` params (static `roles:` params lose to the inner role's `include_vars`).
  Ubuntu 26.04 nodes also need `ansible-core>=2.20`, `ansible_become_exe: /usr/bin/sudo.ws` (sudo-rs
  rejects `-H`) and chrony, since the `ntp` package is gone. See
  `docs/troubleshooting/ansible-ubuntu-2604-compat.md`
- Never let needrestart restart `k3s.service` or `systemd-networkd` on nodes: needrestart < 3.9
  re-flags k3s after every package (restart storm), and a networkd restart flushes Cilium's
  native-routing routes. Both are deferred via `metal/` (`unattended-upgrades`/`networkd` tags).
  See `docs/troubleshooting/needrestart-k3s-restart-storm.md`
- Adding a node: never edit the hardcoded `hosts:` lists in `metal/playbooks/install/prepare.yml`
  (11-13, 18, 23, 30) or `cluster.yml` (28-31) — they are exactly the ZFS/GPU/backup paths a new node
  must skip, so a new node gets no `setup` role. `make first-boot` is unusable (forces root +
  `--ask-pass`) and unscopeable (it appends its own `--limit` last). The node must resolve short names
  or the k3s agent cannot reach `server: https://prusik:6443` — set `prepare_dns_search_domains` if
  DHCP does not push the `grigri` search domain. There is no local StorageClass by deliberate
  decision (a `system/local-path/` chart was considered and dropped — a StorageClass advertises
  unbacked, unquota'd storage on a weak node). On a no-ZFS node any PVC without `storageClassName`
  inherits the default `openebs-zfspv` and hangs `Pending` forever instead of failing; guarded by
  `PersistentVolumeClaimPending` / `PersistentVolumeClaimLost` PrometheusRules. Node-local
  exceptions use `hostPath` with `type: Directory` (Ansible-created paths in `metal/`). See
  `docs/user-guide/add-or-remove-nodes.md` and `planning/k8s-amd64-1-node-addition.md`
- Scoped `metal/` runs (`--limit`) break on templates that read another host's **gathered** facts —
  the control plane is in no play and `delegate_to` does not gather delegate facts, so
  `hostvars[x].ansible_hostname` dies with `object of type 'HostVarsVars' has no attribute ...`. Use
  the inventory name instead. The second symptom is silent: a play that fails discards its pending
  handlers, and the next run sees an unchanged config file so the daemon never reloads. See
  `docs/troubleshooting/ansible-limit-cross-host-facts.md`
- A freshly joined node shows `Ready,SchedulingDisabled` for about a minute: the `k3s-agent`
  system-upgrade Plan has `cordon: true` and selects every non-control-plane node. When the binary
  already matches, the job compares sha256, logs `Binary already been replaced`, exits 0, and the
  controller uncordons. Don't uncordon by hand
- `openebs-zfspv`/`fast-zfspv` use `reclaimPolicy: Retain` — deleting a PV issues no CSI
  DeleteVolume, so the ZFS dataset **and** its `ZFSVolume` CR both survive with no ownerReference to
  GC them. Auditing `Released` PVs therefore reports a clean cluster while the space still leaks;
  the real signal is a `ZFSVolume` CR with no matching PV. 89 volumes / 139.6 GiB accumulated over
  ~9 months this way. Use `scripts/zfs-orphan-audit.sh` (dry-run by default) and the
  `ZFSVolumeOrphaned` alert. Do **not** flip the policy to `Delete` — Retain is the only backstop
  against an ArgoCD prune destroying a database. See `docs/troubleshooting/cluster-hygiene.md`
- `openebs-zfspv` with `fstype: zfs` + `fsGroup` causes slow pod startup (recursive chown on
  every mount). `fsGroupChangePolicy: OnRootMismatch` doesn't help — kubelet resets setgid bit.
  If the app manages its own file ownership (runs as volume owner or has init chown), remove
  `fsGroup` entirely. See `docs/troubleshooting/openebs-zfspv-slow-startup-fsgroup.md`
- Removing `fsGroup` leaves a non-root container with **no** write access to a freshly provisioned
  PVC: a new ZFS dataset mounts `root:root 0755`, kubelet creates missing `subPath` dirs root-owned,
  and `supplementalGroups` changes no ownership. Symptom is `mkdir <path>: permission denied` with a
  healthy, polling pod and nothing in the container log. Migrating off `emptyDir` (which kubelet
  creates 0777) is not a like-for-like swap — it needs an explicit ownership plan. Fix with a root
  init container on the volume **root**, not a subPath. See
  `docs/troubleshooting/ci-runner-workspace-permission-denied.md`
- prusik's L2ARC looks broken (34-39% hit ratio, serves 1.4% of reads) but is load-bearing — avg hit
  size is 17-28KB, i.e. Jellyfin metadata/thumbnails/SQLite, not video. Hit ratio is misleading
  because it only covers ARC misses. The real defect is `l2arc_noprefetch=0` wasting 41% of the device
  on one-shot media prefetch. Fix that; do NOT remove the cache vdev. See
  `docs/troubleshooting/prusik-l2arc-ineffective.md`
- prusik storage is latency-bound, not throughput-bound, and **the root cause is RAM, not disks**:
  container limits sum to **210 GiB** (requests 44.7 GiB) on 62 GiB, forcing `zfs_arc_max=5GB` against
  a 12-14GB hot set; `MemAvailable` bottoms out at 1.97 GiB and 60% of the ARC is metadata. Free
  RAM (move CI off-node) and raise ARC before buying SSDs — RAM beats SSD by ~1000x. Note
  `k8s-amd64-1` has only 16 GiB, and `ci-runner-0` peaks at 17.4 GiB / 7.68 cores, so **CI cannot move
  there until it gets 32 GiB**. A SLOG/ZIL will NOT help (Postgres runs `synchronous_commit=off`).
  Forgejo web latency is `git-upload-pack` reading git objects, not Postgres. Backup is label-driven
  (`backup/retain`, `backup`), so a PVC recreated without labels is silently never backed up. See
  `docs/conventions/prusik-fast-storage-tier.md`
- Apps with Supabase dependencies (auth schema, GoTrue, PostgREST) can use Zalando Postgres +
  init container for bootstrap SQL. Don't deploy separate Supabase Postgres container unless
  the app requires Supabase-specific extensions not in the Spilo image. See
  `docs/deployment/readest.md` and `docs/conventions/deploying-new-apps.md`
- Supabase apps on Zalando Postgres: the `db-migrate` init container automates supporting
  schema/role setup, but upstream `schema.sql` needs GoTrue's `auth` schema (`auth.users`,
  `auth.uid()`) — on a fresh database start GoTrue before the client. See `docs/deployment/readest.md`
- Apps with upstream SQL installed through `docker-entrypoint-initdb.d` need a Zalando-compatible
  init-container bootstrap. For versioned SQL, vendor at the app image tag, regenerate via Renovate,
  review before merge, and guard image/SQL version in CI and at startup. Readest uses a migration
  ledger; Firecrawl replays an idempotent snapshot. See `docs/conventions/deploying-new-apps.md`.
- Apps with init-container workarounds (JS patching, schema health checks) should log version
  info and fail loudly (`exit 1`) on mismatch so Renovate bumps surface breakage immediately.
  Check init container logs after any version bump. See each app's deployment doc for specifics.
- Zalando Postgres PVC recreate causes Patroni stale-DCS deadlock: Patroni DCS ConfigMaps
  (`<cluster>-config`, `<cluster>-leader`) survive PVC deletion and carry the old cluster's
  `initialize` marker, so Patroni waits forever for a leader on a fresh empty data dir. Fix:
  delete the stale DCS ConfigMaps before restarting the pod.
  See `docs/troubleshooting/zalando-patroni-stale-dcs-deadlock.md`
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
- Memory pressure (e.g., CI builds) can crash k3s via etcd timeout, leaving Velero backups stuck
  `InProgress` with orphaned ZFSBackup CRs. Pod restart is the safe first boundary, but residual
  CR/S3/ZFS state must be verified. See `docs/troubleshooting/velero-2026-09-25-oom-etcd-outage.md`
- OpenEBS ZFS 2.11.x `zfs send | nc` can deadlock if `nc` exits early; killing `zfs send` first risks
  marking a truncated volume backup `Done`. Keep the chart pinned to 2.10.1 pending an A/B-tested fix.
  See `docs/troubleshooting/velero-zfs-stream-pipeline-deadlock.md`
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
- Firecrawl's `nuq` queue schema is NOT created by the app — upstream provisions it via a custom
  Postgres image (`docker-entrypoint-initdb.d`). On Zalando/Spilo nobody runs it, so an idempotent
  `nuq-schema-init` init container applies `files/nuq.sql`. On version bump, Renovate runs
  `apps/firecrawl/hack/update-nuq-sql.sh` automatically; pre-commit and deploy-time guards verify
  sync. `cron.database_name` is a restart-required GUC — delete the postgres pod
  once after setting it. See `docs/troubleshooting/firecrawl-selfhost-deployment.md`

## Subsystem Docs

- **Cilium networking:** See `docs/conventions/cilium.md` for BGP, TCX, bandwidth limiting details
- **Deploying new apps:** See `docs/conventions/deploying-new-apps.md` for the decision-making
  process, patterns, and checklist when adding a new application
- **Documenting learnings:** See `docs/conventions/documenting-learnings.md` for when/how to write
  troubleshooting docs
- **prusik storage tiering:** See `docs/conventions/prusik-fast-storage-tier.md` for ZFS pool sizing,
  SSD selection, and PVC migration gotchas. Hardware references: `docs/hardware/prusik.md`,
  `docs/hardware/k8s-amd64-1.md`
- **Adding a node:** `docs/user-guide/add-or-remove-nodes.md` for the runbook,
  `planning/k8s-amd64-1-node-addition.md` for a worked example with measured numbers

## Licensing

GPLv3 (see LICENSE.md). Generated code must be compatible.
