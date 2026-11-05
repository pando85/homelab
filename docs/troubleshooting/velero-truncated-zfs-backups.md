# Velero Truncated ZFS Backup Objects — Netcat Idle Timeout

## Summary

Every scheduled Velero backup since **2026-10-13** is `PartiallyFailed` with 0 successes. The node-side pipeline `zfs send -i <prev> <snap> | nc -w 3 <velero-pod-ip> 9011` uses OpenBSD netcat's `-w 3` **idle** timeout. When the receiver stops draining the socket for >=3s (e.g., during S3 multipart upload backpressure), `nc` exits 0, `zfs send` dies with SIGPIPE (`exit status 141`), and the `clouduploader` receiver in `openebs/velero-plugin:3.6.0` treats the EOF as success and uploads a partial object while logging `successfully uploaded object`. Truncated objects have `DRR_BEGIN=1, DRR_END=0` when inspected with `zstream dump`.

**Last known-good backup:** `retain-quaterly-20261006023052` (Oct 6, phase `Completed`). Its whole incremental chain is structurally complete: Oct 6 → Sep 29 → Sep 28, and for 30 of 32 volumes further back through Sep 26 → Aug 25 → Aug 18 → Aug 11. All verified `DRR_END=1`.

**Time-critical:** `retain-quaterly-20260811023053` is the chain ROOT for at least one volume and **expires 2026-11-09**. With `incrBackupCount: 11` a full arrives every 12 runs; on a weekly schedule that is 84 days against a `2160h` (90-day) TTL — **about 6 days of margin**, and that margin only holds if every scheduled run succeeds. The Oct 13 – Nov 4 failures consumed it: no run succeeded, so no replacement full was ever produced, and the Aug 11 root aged out on schedule. This is a latent design flaw, independent of the nc bug.

## Symptom and Detection Signature

- Velero backups are `PartiallyFailed` with errors like:
  ```
  error taking snapshot of volume: rpc error: code = Unknown desc =
    zfs: error in uploading snapshot, status:{Failed}
  ```
- Node agent logs show:
  ```
  zfs: could not backup <vol> snap <snap> bkpname <name>
    cmd [zfs send -i <prevsnap> <cursnap>] | [nc -w 3 <receiver-ip> 9011]
    error: ... pipe source [...]: exit status 141
  ```
  (SIGPIPE = 128 + 13)
- Velero plugin (receiver) logs show, for the **same** transfer:
  ```
  Transfer done.. closing the server
  successfully uploaded object{...}
  ```
- S3 objects are present but structurally incomplete: `zstream dump` shows `DRR_BEGIN=1` but `DRR_END=0` (missing end record).

**Why this is dangerous:** The sender (`exit status 141`), the receiver (`successfully uploaded object`) and the backup metadata disagree about the same transfer. The receiver logging success is why the truncation is silent — the object lands in S3 and looks complete unless you inspect its stream structure.

### Detection Recipe

```bash
# List recent backups and their phase
kubectl --context=grigri -n velero get backups --sort-by=.metadata.creationTimestamp | tail -20

# Check ZFSBackup CRs for Failed or missing Done status
kubectl --context=grigri -n zfs-localpv get zb \
  -o custom-columns='NAME:.metadata.name,VOLUME:.spec.volumeName,STATUS:.status,PREV:.spec.prevSnapName'

# Node agent logs for SIGPIPE
kubectl --context=grigri -n zfs-localpv logs -l app=openebs-zfs-node --tail=500 | grep -E 'exit status 141|pipe source'

# Velero plugin logs for "successfully uploaded object" on small objects
kubectl --context=grigri -n velero logs -l app.kubernetes.io/name=velero --tail=500 | grep 'successfully uploaded object'
```

**Note:** `kubectl logs` only covers the current container lifetime. The node pods restarted ~16h before the last failing run (2026-11-04 03:30), so the commands above may return nothing for older runs. Loki (retention begins ~2026-10-06) is the place to look for older failures:

```logql
{job="systemd-journal", node_name=~".+"} |~ "exit status 141|pipe source"
```

## Root Cause

The OpenEBS ZFS driver **v2.11.2** runs the backup pipeline as `zfs send -i <prev> <snap> | nc -w 3 <velero-pod-ip> 9011`. The `-w 3` is an **idle** timeout: if no data flows through the socket for 3 seconds, `nc` exits 0. The receiver (Velero plugin's `clouduploader`) closes its object writer when the client disconnects, even if the ZFS stream is incomplete. The plugin polls `ZFSBackup.status` and treats the uploaded object as success, logging `successfully uploaded object`.

### Per-Version Behavior

| Version | Behavior | Exit Code | Notes |
|---------|----------|-----------|-------|
| <=2.10.1 | Masks truncation | 0 | Runs pipeline as `bash -c "zfs send \| nc"` without `pipefail`, so sender's SIGPIPE is lost and truncated objects are reported as success. **Do not downgrade to this version** — it hides the bug. |
| 2.11.0–2.11.1 | Hangs in `Init` | N/A | `runPipe` waits for `zfs send` before `nc`, so if `nc` exits early, `zfs send` blocks on its pipe indefinitely. `ZFSBackup` stays `Init`. See `velero-zfs-stream-pipeline-deadlock.md`. |
| 2.11.2 | Reports `Failed` | 141 | `runPipe` waits for both children concurrently and joins exit errors. Sender's SIGPIPE is detected and reported. **This is the first version that surfaces the bug visibly.** |

**Upstream issue:** [openebs/zfs-localpv#773](https://github.com/openebs/zfs-localpv/issues/773) (reopened).

**Fix:** Branch `fix/configurable-netcat-timeout` (commit `28f9f96`) on `pando85/zfs-localpv` adds env var `ZFS_NETCAT_TIMEOUT`, default **300s**, applied to both `buildVolumeBackupArgs` and `buildVolumeRestoreArgs`. It cherry-picks cleanly onto tag `v2.11.2`. The fix image is published at `ghcr.io/pando85/zfs-driver:2.11.2-nc-timeout` (digest `sha256:c6a677d4665524408fe8de8e278ead3cb68419853d57200f4485332cc13fb02c`, linux/amd64) — see Recovery Procedure step (a).

## Verification Recipe: `zstream dump`

**WARNING:** `zfs send -nvP` size comparison is **unreliable below a few MB**. We measured a 6.7x overshoot on a complete 624-byte object, and three false-positive "truncations" on sub-2 MB streams. Use `zstream dump` to count `DRR_BEGIN`/`DRR_END` records instead.

### Structural Verification

```bash
# List objects for a specific volume in a backup (the middle component is not trivially
# reconstructable from the volume name alone, so list and grep rather than construct the key).
# Every stream object has a .zfsvol sibling (~1.7 KB metadata), so filter it out or you will
# try to zstream dump the metadata file.
# Retrieve credentials from pass or Vault, not CLI args.
# aws --endpoint-url=https://s3.internal.grigri.cloud --profile velero \
#   s3 ls s3://velero/backups/<backup-name>/ | grep <volume-name> | grep -v '\.zfsvol$'

# Download the stream object
# aws --endpoint-url=https://s3.internal.grigri.cloud --profile velero \
#   s3 cp s3://velero/backups/<backup-name>/<object-key> /tmp/<volume>.zfs

# Count DRR records
zstream dump /tmp/<volume>.zfs | grep -c '^DRR_BEGIN'  # Should be 1
zstream dump /tmp/<volume>.zfs | grep -c '^DRR_END'    # Should be 1 for complete, 0 for truncated

# A complete stream has DRR_BEGIN=1, DRR_END=1.
# A truncated stream has DRR_BEGIN=1, DRR_END=0.
```

**Object key layout:** The VSL `prefix: zfs` does **not** create a `zfs/` directory — it prefixes the object *names* inside the per-backup directory. The bucket root has only `backups/` and `restores/`. The real key format is:

```
s3://velero/backups/<backup-name>/zfs-…-<volume-name>-<backup-name>          # ZFS send stream
s3://velero/backups/<backup-name>/zfs-…-<volume-name>-<backup-name>.zfsvol   # ~1.7 KB metadata
```

Example real key: `zfs-retain-quaterly-antdroid-data-retain-quaterly-20261006023052`.

### Size Comparison (Unreliable for Small Streams)

```bash
# Estimate the expected size (unreliable < 2 MB)
ssh <node> 'zfs send -nvP -i <prev-snap> <snap>'

# Compare with the object size in S3 (list and grep, do not construct the key)
# aws --endpoint-url=https://s3.internal.grigri.cloud --profile velero \
#   s3 ls s3://velero/backups/<backup-name>/ | grep <volume-name>
```

For small volumes (< 2 MB), the estimate can overshoot by 6x or more. Do not use size comparison alone to declare truncation; use `zstream dump` for structural verification.

## Recovery Procedure

All mutating commands are marked **[MUTATING]**. The owner should review and execute these steps manually.

The `velero` CLI is installed at `/usr/bin/velero`. Every `velero` invocation below carries `--kubecontext grigri -n velero`.

### Step (a): Prerequisite — the Fix Image

The fix image is already built and published. No local build or manual push is required.

- **Image:** `ghcr.io/pando85/zfs-driver:2.11.2-nc-timeout`
- **Digest:** `sha256:c6a677d4665524408fe8de8e278ead3cb68419853d57200f4485332cc13fb02c`
- **Platform:** linux/amd64, 5 layers, OCI manifest (`application/vnd.oci.image.manifest.v1+json`). All three cluster nodes are amd64, so single-arch is sufficient.
- **Public and anonymously pullable:** an anonymous manifest GET returns HTTP 200. The cluster needs **no `imagePullSecret`** — this is why the `values.yaml` pin works as-is.
- **Patch verified in the published artifact:** extracting `/usr/local/bin/zfs-driver` (76,371,717 bytes) from the pulled image and grepping finds `ZFS_NETCAT_TIMEOUT` and `netCatTimeout`. The workflow performs this check as a post-push step and fails the run if the string is absent, so a silently unpatched image cannot be published.
- **Source:** upstream tag `v2.11.2` (`24bbd89`) plus commit `7bb8c18` (`fix(backup): make the netcat idle timeout configurable`), which is a clean cherry-pick of `28f9f96` from branch `fix/configurable-netcat-timeout`.
- **Compiled default:** `ZFS_NETCAT_TIMEOUT` defaults to 300s when the env var is unset. The chart exposes no `env` hook for the node plugin container, so the compiled default is what applies.

**How it was built:** Workflow `Fork image build` at `.github/workflows/fork-build-push.yml` in `pando85/zfs-localpv`, on branch `fork/2.11.2-nc-timeout`, run `41893612196` (conclusion `success`).

**How to rebuild:** Push any `fork/**` branch to the fork, or trigger the workflow manually via `workflow_dispatch` with the `image_tag` input (default `2.11.2-nc-timeout`). The tag is derived from the branch name with the `fork/` prefix stripped. Do **not** derive it from `deploy/helm/charts/Chart.yaml` — at tag `v2.11.2` that file reads `version: 2.11.2-prerelease`, which would produce the wrong tag.

**CI credential requirement:** `GITHUB_TOKEN` cannot push to `ghcr.io/pando85/*` from this repository because it is a fork; the push fails with `denied: permission_denied: write_package`. This was diagnosed empirically — repo Actions is enabled, repo workflow permissions are `write`, the workflow declares `permissions: packages: write`, the package pre-existed, was public and was linked to the repo, and both plain `docker push` and buildx push failed identically, which ruled out credential plumbing. Adding the `org.opencontainers.image.source` label did not fix it either. The workflow therefore authenticates with the repository secret `GHCR_PAT` (a classic PAT with `write:packages`). GitHub's Container registry docs state that GHCR only supports authentication with a **classic** PAT — fine-grained PATs do not work.

**Leftover bootstrap tag:** During package bootstrapping, an `alpine:3.23.5` image was pushed as tag `_bootstrap` purely to create the GHCR package and make it public. It is **not** a driver image and must not be used. It still exists and should be deleted from the package settings UI; the available token lacks `delete:packages`, so it cannot be removed via API. **Hazard:** pulling `ghcr.io/pando85/zfs-driver:_bootstrap` yields an Alpine shell, not a CSI driver.

### Step (b): Let ArgoCD Sync the Pinned Image

The chart change in this commit pins `zfsPlugin.image` to `ghcr.io/pando85/zfs-driver:2.11.2-nc-timeout`. ArgoCD will sync the new image automatically.

```bash
# Verify the sync — confirm the DaemonSet pods are running the fork image
kubectl --context=grigri -n zfs-localpv get pods -o wide

# Check the running container image (the openebs-zfs-plugin container, not the sidecar)
kubectl --context=grigri -n zfs-localpv get pods -l app=openebs-zfs-node \
  -o jsonpath='{range .items[*]}{.spec.nodeName}{"\t"}{.spec.containers[?(@.name=="openebs-zfs-plugin")].image}{"\n"}{end}'
```

### Step (c): Verify the Running Image and Behaviour

```bash
# Confirm the fork image is running (image tag must be 2.11.2-nc-timeout)
kubectl --context=grigri -n zfs-localpv get pods -l app=openebs-zfs-node \
  -o jsonpath='{range .items[*]}{.spec.nodeName}{"\t"}{.spec.containers[?(@.name=="openebs-zfs-plugin")].image}{"\n"}{end}'

# The fork image uses a compiled default of 300s for ZFS_NETCAT_TIMEOUT.
# There is no env var to inspect — the value is baked into the binary.
# The real proof is behavioural: on the next scheduled run, confirm no
# "exit status 141" in node agent logs and all zb CRs reach Done.
```

If the image tag does not match `2.11.2-nc-timeout`, ArgoCD has not synced yet. Do not proceed to step (d) until the fork image is confirmed running on all eligible nodes.

### Step (d): Delete Post-Oct-6 Backup CRs, Chain-Aware **[MUTATING]**

Use `velero backup delete`, **not** `kubectl delete backup`. The Velero CLI creates a `DeleteBackupRequest`, which drives the openebs plugin's `DeleteSnapshot` so the ZFS snapshots and the S3 objects are actually reclaimed. `kubectl delete backup` removes only the CR and orphans both.

**Note:** The two stuck `Deleting` backups (`retain-quaterly-20260728023032`, `retain-quaterly-20260804023040`) are already covered by follow-up #1; deleting them again is not part of this recovery. The irony is that these stuck backups show `DeleteBackupRequest` can itself stall — confirm each deletion actually completes rather than assuming it.

**Verified backup inventory** (`kubectl --context=grigri -n velero get backups`):

| Backup name | Phase | Expiration |
|---|---|---|
| `retain-quaterly-20260728023032` | Deleting (stuck ~99d) | 2026-10-26 |
| `retain-quaterly-20260804023040` | Deleting (stuck ~92d) | 2026-11-02 |
| `retain-quaterly-20260811023053` | Completed | **2026-11-09** |
| `retain-quaterly-20260818023002` | Completed | 2026-11-16 |
| `retain-quaterly-20260825023011` | Completed | 2026-11-23 |
| `retain-quaterly-20260926081711` | PartiallyFailed | 2026-12-25 |
| `retain-quaterly-20260928135503` | Completed | 2026-12-27 |
| `retain-quaterly-20260929023005` | Completed | 2026-12-28 |
| `retain-quaterly-20261006023052` | Completed | 2027-01-04 |
| `retain-quaterly-20261013023021` | Failed | 2027-01-11 |
| `retain-quaterly-20261102042133` | PartiallyFailed | 2027-01-31 |
| `retain-quaterly-20261104033038` | PartiallyFailed | 2027-02-02 |
| `retain-weekly-20261013023021` | PartiallyFailed | 2026-11-12 |
| `retain-weekly-20261020023030` | PartiallyFailed | 2026-11-12 |
| `retain-weekly-20261027023038` | PartiallyFailed | 2026-11-12 |
| `retain-weekly-20261103023034` | PartiallyFailed | 2026-11-13 |

**PRESERVE (7):**
- `retain-quaterly-20260811023053` — chain root for at least one volume, **expires 2026-11-09**
- `retain-quaterly-20260818023002`
- `retain-quaterly-20260825023011`
- `retain-quaterly-20260926081711` — **PartiallyFailed but MUST be preserved** (see note below)
- `retain-quaterly-20260928135503`
- `retain-quaterly-20260929023005`
- `retain-quaterly-20261006023052` — last known-good

**DELETE (7)** — all post-Oct-6, both schedules:
- `retain-quaterly-20261013023021`
- `retain-quaterly-20261102042133`
- `retain-quaterly-20261104033038`
- `retain-weekly-20261013023021`
- `retain-weekly-20261020023030`
- `retain-weekly-20261027023038`
- `retain-weekly-20261103023034`

**Note on the four weekly targets:** Because the weekly schedule is bound to `zfspv-full` (consequence #4 in step (e)), the four `retain-weekly-*` entries above are standalone fulls, not chain links — deleting them cannot break any restore path. Furthermore their TTL is only `240h` (10 days), so three expire **2026-11-12** and the fourth **2026-11-13**; they will self-clean within about a week of writing. Deleting them is optional tidiness. The part that actually matters is deleting the three post-Oct-6 `retain-quaterly-*` entries, because those are the ones whose truncated objects and stale `Done`/`Failed` CRs would otherwise be eligible as incremental parents.

```bash
# Delete each post-Oct-6 backup **[MUTATING]**
# velero backup delete --kubecontext grigri -n velero --confirm retain-quaterly-20261013023021
# velero backup delete --kubecontext grigri -n velero --confirm retain-quaterly-20261102042133
# velero backup delete --kubecontext grigri -n velero --confirm retain-quaterly-20261104033038
# velero backup delete --kubecontext grigri -n velero --confirm retain-weekly-20261013023021
# velero backup delete --kubecontext grigri -n velero --confirm retain-weekly-20261020023030
# velero backup delete --kubecontext grigri -n velero --confirm retain-weekly-20261027023038
# velero backup delete --kubecontext grigri -n velero --confirm retain-weekly-20261103023034

# Verify the preserved backups are still present
kubectl --context=grigri -n velero get backups | grep -E '20260811|20260818|20260825|20260926|20260928|20260929|20261006'
```

**Chain topology note:** `retain-quaterly-20260926081711` is `PartiallyFailed` but MUST still be preserved. Sep 28 is a FULL for only 2 volumes and an incremental off Sep 26 for the other 30, so Sep 26 is a load-bearing chain link despite its phase. Its one truncated object belongs to a volume that got a fresh full at Sep 28, so the truncated object is orphaned dead weight, not a broken link. Phase alone does not tell you whether a backup is safe to delete.

Sep 28, Sep 29 and Oct 6 are 100% incremental for the remaining volumes. The chain is per-volume, so some volumes have longer chains than others.

### Step (e): Force a Fresh FULL via `incrBackupCount` **[MUTATING]**

**Do NOT delete `Done` ZFSBackup CRs to force a full.** That destroys the underlying ZFS snapshots (see "Do Not Do This" below). The source-verified mechanism is to temporarily set `incrBackupCount: "0"` in the VolumeSnapshotLocation config.

**Source:** `getPrevSnap` in `pkg/zfs/plugin/backup.go` (openebs/velero-plugin **v3.6.0**, commit `cea57783`), lines 79–117:

```go
func (p *Plugin) getPrevSnap(volname, schdname string) (string, error) {
	if p.incremental < 1 || len(schdname) == 0 {
		// not an incremental backup, take the full backup
		return "", nil
	}
	listOptions := metav1.ListOptions{
		LabelSelector: VeleroSchdKey + "=" + schdname + "," + VeleroVolKey + "=" + volname,
	}
	bkpList, err := bkpbuilder.NewKubeclient().WithNamespace(p.namespace).List(listOptions)
	...
	size := len(bkpList.Items)
	count := p.incremental + 1
	if uint64(size)%count == 0 {
		// have to start the next snapshot incremental group, take the full backup
		return "", nil
	}
	// otherwise: pick the lexicographically last SnapName among CRs with Status == Done
}
```

`p.incremental` is set in `pkg/zfs/plugin/zfs.go` lines 68–74 from the VSL config key `incrBackupCount` (`ZfsPvIncr`), parsed as `uint64`.

**Consequences:**

1. **`incrBackupCount: "0"` forces a FULL — VERIFIED.** `0 < 1` short-circuits `getPrevSnap` to `""` before any modulo arithmetic. No division by zero. If the key is **absent** entirely, `p.incremental` keeps Go's zero value `0`, so absent also means always-full.
2. With the production value `"11"`, `count = 12`, so a full happens when `len(CR list) % 12 == 0` — i.e. **11 incrementals between fulls**. The count is `len(bkpList.Items)`, which **includes CRs from Failed runs**, and is filtered only by the labels `velero.io/schedule-name=<schedule>` and `velero.io/volname=<volume>` — **not** by status. Only the `prevSnapName` *selection* filters on `Status == Done`. The post-Oct-6 failed runs therefore shifted the modulo, which is part of why no natural full arrived. **Caveat:** step (d) deletes those failed backups via `velero backup delete`, which drives the plugin's `DeleteSnapshot` path; if that removes the corresponding ZFSBackup CRs (inferred from the plugin's delete path, **not verified from source** — the openebs/velero-plugin v3.6.0 source is not available locally to confirm), the CR count drops and the modulo position shifts again. Either way, the forced-full route below is the reliable fix.
3. **A manual backup that is not triggered by a Schedule has an empty `schdname`, so it is ALWAYS a full** — the `len(schdname) == 0` branch. But because the label selector includes `schedule-name`, a manual full is **not** visible to subsequent scheduled runs as a parent, so it creates a standalone restorable anchor without re-basing the scheduled chain. To re-base the scheduled chain, use Route A or Route B below.
4. **`zfspv-full` is special by design — the two schedules bind to different VSLs.** `schedule-retain-quaterly.yaml` binds to `volumeSnapshotLocations: [zfspv-incr]` (which carries `incrBackupCount: "11"`), while `schedule-retain-weekly.yaml` binds to `volumeSnapshotLocations: [zfspv-full]` (which has no `incrBackupCount` key at all — verified by `grep -n "incrBackupCount" platform/velero/values.yaml`, single hit at line 74 inside the `zfspv-incr` block). Because `getPrevSnap` returns `""` (full) when `p.incremental < 1`, and `p.incremental` defaults to Go's zero value `0` when the key is absent, **every `retain-weekly-*` backup is a standalone FULL by construction**, not a link in any incremental chain. Only `retain-quaterly-*` backups form an incremental chain. The VSL `zfspv-full` needs no change to produce a full — that is evidently its purpose.

**Trade-off:** A full backup costs on the order of the Sep 28 backup (~769 GB for all volumes). Continuing the incremental from Oct 6 would be cheaper but risks chain corruption if any intermediate is missing or if the Oct 6 chain root expires (Nov 9 for Aug 11). **Forcing a fresh full is safer and simpler.**

**Both routes below must go through git, not `kubectl patch`.** This repo is GitOps with ArgoCD `selfHeal: true`; a `kubectl patch` on the VolumeSnapshotLocation or the Schedule would be reverted almost instantly.

**Why either route re-bases the chain:** `getPrevSnap` keys the CR lookup on the labels `velero.io/schedule-name=<schdname>` and `velero.io/volname=<vol>`, and `createBackup` sets those labels whenever `schdname` is non-empty — the VSL only supplies `p.incremental`. So a quarterly run performed through `zfspv-full` still creates a ZFSBackup CR labelled `schedule-name=retain-quaterly`, and the next quarterly run through `zfspv-incr` will select it as `prevSnapName` (it picks the lexicographically last `Done` `SnapName`). The chain re-bases onto the new full regardless of which VSL produced it.

#### Route A (recommended) — point the quarterly schedule at `zfspv-full` for one run **[MUTATING]**

A one-line change to `platform/velero/templates/schedule-retain-quaterly.yaml`: `volumeSnapshotLocations: [zfspv-incr]` → `[zfspv-full]`. This uses the VSL that already exists for exactly this purpose (consequence #4 above) and changes no numeric value.

**Commit 1 — switch VSL [MUTATING]:**

```bash
# Edit platform/velero/templates/schedule-retain-quaterly.yaml:
# change volumeSnapshotLocations from [zfspv-incr] to [zfspv-full]
sed -i 's/- zfspv-incr/- zfspv-full/' platform/velero/templates/schedule-retain-quaterly.yaml
git add platform/velero/templates/schedule-retain-quaterly.yaml
git commit -m 'velero: Force quarterly full via zfspv-full VSL for one run'
git push
# Wait for ArgoCD to sync, then wait for the next scheduled quarterly run to complete.
# Verify:
velero backup get --kubecontext grigri -n velero
kubectl --context=grigri -n zfs-localpv get zb | grep -v Done
```

**Commit 2 — revert VSL [MUTATING]:**

```bash
# After the full backup lands (all zb CRs reach Done, backup phase is Completed):
sed -i 's/- zfspv-full/- zfspv-incr/' platform/velero/templates/schedule-retain-quaterly.yaml
git add platform/velero/templates/schedule-retain-quaterly.yaml
git commit -m 'velero: Restore quarterly schedule to zfspv-incr VSL'
git push
```

Prefer this route because reverting a `volumeSnapshotLocations` list is harder to get wrong than reverting a counter — there is no numeric value that could silently be left at the wrong setting and turn every future quarterly run into a 769 GB full.

#### Route B — temporarily set `incrBackupCount: "0"` on `zfspv-incr` **[MUTATING]**

Behaviourally identical to Route A (`p.incremental = 0` either way), but leaves a live numeric value that must be remembered and restored.

**WARNING:** Leaving `incrBackupCount` at `"0"` makes every scheduled quarterly run a full (~769 GB each). Revert immediately after the full lands. Forgetting to revert is the main risk of this route.

**Commit 1 — set `incrBackupCount: "0"` [MUTATING]:**

```bash
# Edit platform/velero/values.yaml: under zfspv-incr.config, change incrBackupCount to "0"
sed -i 's/incrBackupCount: "11"/incrBackupCount: "0"/' platform/velero/values.yaml
git add platform/velero/values.yaml
git commit -m 'velero: Force full backup by setting incrBackupCount to 0'
git push
# Wait for ArgoCD to sync, then wait for the next scheduled run to complete.
# Verify:
velero backup get --kubecontext grigri -n velero
kubectl --context=grigri -n zfs-localpv get zb | grep -v Done
```

**Commit 2 — revert to `"11"` [MUTATING]:**

```bash
# After the full backup lands (all zb CRs reach Done, backup phase is Completed):
sed -i 's/incrBackupCount: "0"/incrBackupCount: "11"/' platform/velero/values.yaml
git add platform/velero/values.yaml
git commit -m 'velero: Restore incrBackupCount to 11 after forced full'
git push
```

### Step (f): Verify the New Backup is `Completed` and Spot-Check `DRR_END`

```bash
# Wait for the backup to complete
velero backup get --kubecontext grigri -n velero | tail -5

# Check all ZFSBackup CRs reached Done
kubectl --context=grigri -n zfs-localpv get zb | grep -v Done

# Download a few objects and verify DRR_END=1 (list and grep for the key, do not construct it)
# aws --endpoint-url=https://s3.internal.grigri.cloud --profile velero \
#   s3 ls s3://velero/backups/<new-backup-name>/ | grep <volume-name> | grep -v '\.zfsvol$'
# aws --endpoint-url=https://s3.internal.grigri.cloud --profile velero \
#   s3 cp s3://velero/backups/<new-backup-name>/<object-key> /tmp/<volume>.zfs
# zstream dump /tmp/<volume>.zfs | grep -c '^DRR_END'  # Should be 1
```

### Step (g): Restore Test into a Scratch Namespace

```bash
# Create a scratch namespace
kubectl --context=grigri create namespace restore-test

# Restore a few volumes into the scratch namespace **[MUTATING]**
# velero restore create --kubecontext grigri -n velero \
#   --from-backup <new-backup-name> \
#   --namespace-mappings '<original-ns>:restore-test' \
#   --include-resources persistentvolumes,persistentvolumeclaims

# Verify the restored PVCs are bound and the data is intact
kubectl --context=grigri -n restore-test get pvc
# kubectl --context=grigri -n restore-test exec -it <pod> -- ls -la /data

# Clean up the scratch namespace when done **[MUTATING]**
# kubectl --context=grigri delete namespace restore-test
```

## Do Not Do This

- **Do NOT downgrade zfs-localpv to 2.10.1.** That version runs the pipeline as `bash -c "zfs send | nc"` without `pipefail`, so the sender's SIGPIPE is masked and truncation is reported as success. It is the version that **hid** this bug, not the version that avoids it. See the per-version behavior table above.

- **Do NOT lower `multiPartChunkSize` below `100Mi`.** MinIO's `MaxUploadParts=10000` caps object size at `chunkSize x 10000`. The largest full send already consumes most of that budget (~769 GB). Reducing `multiPartChunkSize` would break large fulls with `TotalPartsExceeded` (openebs/velero-plugin#185, openebs/velero-plugin#188). Keep it at `100Mi`.

- **Do NOT delete `Done` ZFSBackup CRs to force a full backup.** The `ZFSBackup` resource carries the finalizer `zfs.openebs.io/finalizer`; on deletion the backup controller's `syncBkp` sees `DeletionTimestamp`, calls `zfs.DestoryBackup(bkp)` (upstream typo is real), which builds a `ZFSSnapshot` from `bkp.Spec.SnapName` and runs `zfs destroy <pool>/<vol>@<snapname>`, then removes the finalizer. **Deleting a ZFSBackup CR destroys the underlying ZFS snapshot.** Doing that for the Aug 11 → Oct 6 chain makes those backups permanently unrestorable. Use Route A (switch VSL) or Route B (`incrBackupCount: "0"`) in step (e) instead.

## Follow-Ups (Separate Defects)

These defects were found during the investigation and should be addressed separately:

1. **Two backups stuck in phase `Deleting` for 92-99 days** (`retain-quaterly-20260728023032`, `retain-quaterly-20260804023040`) with no `deletionTimestamp` and no finalizers, despite their `DeleteBackupRequest` showing `Processed`. No clear recovery path; may require manual CR deletion and S3 cleanup.

2. **~43 orphaned backup prefixes in the bucket** with no corresponding Backup CR; 1008 objects total, 864 zfs-prefixed, 106 under 1 KB. Requires S3 cleanup with `mc rm --recursive --force --versions` or equivalent.

3. **~739 leftover ZFS snapshots on the storage node** dating to Aug 11. Requires manual cleanup with `zfs destroy` after verifying no ZFSBackup CR references them.

4. **Prometheus serves no data at all** (even `up` returns empty) and Grafana has **zero alert rules** — which is why a 3-week total backup outage was never paged. Requires Prometheus/Grafana investigation and alert rule creation.

5. **13 stateful zfs-localpv PVCs have no `backup/retain` label** and are silently excluded, including gitaly repo storage and several Spilo postgres volumes. Requires labeling the PVCs and verifying they are included in subsequent backups.

6. **Retention margin is structurally fragile.** The 34 PVCs labelled `backup/retain: quaterly` are the large volumes and they only ever receive incrementals except on every 12th run; the 14 PVCs labelled `backup/retain: weekly` receive a full every week but with a 10-day TTL, so those fulls expire before they can serve as an archive. The label is single-valued, so no volume is in both sets. The consequence is that the big data depends entirely on one incremental chain whose root expires at 90 days while a replacement full only arrives every 12 weekly runs (84 days) — about 6 days of margin, and that margin only holds if every run succeeds. The Oct 13 – Nov 4 failures consumed it: `retain-quaterly-20260811023053` is expiring on 2026-11-09 with no replacement full in place. This is a latent design flaw independent of the netcat bug. Consider shortening the full cycle, lengthening the TTL, or adding a second full anchor.

## References

- General Velero recovery runbook: `docs/troubleshooting/velero-backup-failures.md`
- Velero stream pipeline deadlock (2.11.0-2.11.1 hang): `docs/troubleshooting/velero-zfs-stream-pipeline-deadlock.md`
- Velero schedules: `platform/velero/templates/schedule-retain-weekly.yaml`, `schedule-retain-quaterly.yaml`
- Velero VolumeSnapshotLocation config: `platform/velero/values.yaml` (lines 44-80)
- Upstream issue: [openebs/zfs-localpv#773](https://github.com/openebs/zfs-localpv/issues/773)
- Fix branch: `pando85/zfs-localpv` branch `fix/configurable-netcat-timeout` (commit `28f9f96`)
- Fork image build workflow: `pando85/zfs-localpv` `.github/workflows/fork-build-push.yml`, branch `fork/2.11.2-nc-timeout`
- Published fix image: `ghcr.io/pando85/zfs-driver:2.11.2-nc-timeout` (digest `sha256:c6a677d4665524408fe8de8e278ead3cb68419853d57200f4485332cc13fb02c`)
- MinIO architecture: `docs/deployment/minio-architecture.md`
