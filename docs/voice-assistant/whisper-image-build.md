# Building the Kivi Whisper STT image on Forgejo

**Status:** plan; the image repository exists, **nothing is published yet** (no tag, no registry push, no
workflow run) and no cluster or Home Assistant change was made.
**Date:** 2026-11-05
**Goal:** replace `pando85/wyoming-whisper:2.4.0` with an image built on
`wyoming-faster-whisper[hass]==3.8.1`, so the STT step gets the live Home Assistant name list
(`--hass-token`) and the static Spanish verb prefix (`--initial-prompt`), as designed in
[STT hint list and learning from usage](stt-hint-list-and-learning.md) (phase 1).

Evidence labels: **documented**, **observed**, **inferred**, **hypothesis** (as in the design doc).

## Where the build lives

Everything about building the image is in the repository **`agil/wyoming-whisper`**
(<https://git.grigri.cloud/agil/wyoming-whisper>, private, created 2026-11-05): the Dockerfile and the
hashed lock, the Forgejo Actions workflow (build and push only on a tag), the transcript-log patch
(transcripts at DEBUG, not INFO), the Renovate configuration, and the docs on the build pipeline, the
sm_61 check, the runner disk, the risks and the image rollback. This page keeps only what belongs to the
cluster.

Summary of the decisions (details and evidence in that repository):

- Base `ghcr.io/opennmt/ctranslate2:4.8.5-ubuntu22.04-cuda12.8` pinned by digest, Python 3.10,
  `wyoming-faster-whisper[hass]==3.8.1`, `faster-whisper` 1.2.1, `ctranslate2` 4.8.5.
- GTX 1060 (sm_61): the driver 580 is the last Pascal branch, so the image stays on CUDA 12.x.
  `cuobjdump` on the local build shows `sm_61` SASS in the CTranslate2 library (static evidence, 2026-11-05;
  the canary below is the gate). The local build is about 3.2 GB uncompressed, about 2 GB compressed.
- Tags are `<upstream version>.<revision>` (first planned: `3.8.1.1`), created by a human after the canary;
  no `latest`.
- Pull access: the nodes' containerd already has a read-only credential for `git.grigri.cloud`
  (`metal/roles/k3s/templates/host.toml.j2`, the owner's own account, pull/resolve only), so a private
  package owned by that account needs no `imagePullSecret` here. Observed for private `multivara/*`
  images; a pull of this package is **not yet verified** (no image exists).
- Fallback if the runners' disks are at risk: build on GitHub and publish to `ghcr.io/pando85/wyoming-whisper`
  (only the image name changes here).

## What the owner must do before the first tag

1. Create a Forgejo token with only `write:package`, keep it in `pass` (`0600` file) and set it as the
   repository secret `FORGEJO_PACKAGES_PAT` of `agil/wyoming-whisper`.
2. Optional: the Renovate secrets `RENOVATE_PAT` and `RENOVATE_GITHUB_COM_TOKEN` for that repository, then
   enable the schedule in its `renovate.yml`.
3. Approve the first tag (`v3.8.1.1`); afterwards set the package cleanup rule (keep the last 3 versions).

## Cluster plan

Steps marked **[owner]** need the owner; **[approval]** can be done by an agent after an explicit yes.

### Phase B: GPU canary on `prusik` (before touching the real STT)

The card has 3 GB with about 1.1 GB in use, so do **not** run two large-v3-turbo instances. A tiny model
needs well under 300 MiB. The check itself is described in the image repository
(`docs/sm61-verification.md`); here is the cluster part.

1. [approval, GitOps] Commit a temporary controller `whisper-canary` in `apps/home-assistant/values.yaml`:
   the new image (`tag@sha256` from the job summary), args `--model tiny-int8 --language es --device cuda
   --compute-type int8 --debug`, `nvidia.com/gpu: "1"`, `runtimeClassName: nvidia`, no Service and no
   Ingress, `emptyDir` for `/data` (it downloads the tiny model; needs egress to Hugging Face).
2. [owner or approved exec] Read-only checks in the pod: `python3 -c "import ctranslate2 as c;
   print(c.get_cuda_device_count(), c.get_supported_compute_types('cuda'))"` (expect 1 and `int8` in the
   list) and the pod log. A Wyoming transcribe of a short Spanish clip of the owner's own voice through
   `kubectl port-forward` is the end-to-end test; delete the clip afterwards. `--debug` also logs
   transcripts: use it briefly and do not paste the output.
3. Pass: no CUDA error such as `no kernel image is available for execution on the device`, `int8`
   supported, one transcript returned. Record the result in the image repository's
   `docs/sm61-verification.md`.
4. Remove the canary controller (revert that commit).

### Phase C: cutover (homelab GitOps)

All three changes in one commit `home-assistant: Run whisper 3.8.1 with live Home Assistant names` (or
split into an inert ExternalSecret and ConfigMap commit first), pushed only with the owner's yes.

**C1. Token Secret** (Vault path already exists, see
[the Home Assistant token](stt-hint-list-and-learning.md#the-home-assistant-token): `secret/home-assistant/wyoming-whisper`, key `hass_token`):

`apps/home-assistant/templates/external-secret-whisper.yaml`
```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: whisper-hass-token
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: vault
  target:
    name: whisper-hass-token
  data:
    - secretKey: hass_token
      remoteRef:
        key: /home-assistant/wyoming-whisper
        property: hass_token
```

**C2. Prompt ConfigMap** `apps/home-assistant/templates/configmap-whisper-prompt.yaml` (generic Spanish
verbs only, no home names, no "Kivi"; text from section 2.2 of the design doc, changing it needs the
study's comparison):
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: whisper-prompt
data:
  initial-prompt.txt: >-
    Enciende, apaga, sube, baja, abre, cierra, para, pausa, reanuda, silencia, quita el silencio,
    pon, temporizador, minutos, volumen, brillo, por ciento, grados.
```

**C3. `apps/home-assistant/values.yaml`, controller `whisper`**
```yaml
    whisper:
      type: deployment
      replicas: 1
      annotations:
        reloader.stakater.com/auto: "true"      # roll on ConfigMap/Secret change (Recreate: a short STT gap)
      ...
      containers:
        whisper:
          image:
            repository: git.grigri.cloud/agil/wyoming-whisper
            tag: 3.8.1.1@sha256:<digest from the A5 job summary>
          args:
            - --model
            - large-v3-turbo
            - --language
            - es
            - --device
            - cuda
            - --compute-type
            - int8
            - --hass-api
            - http://home-assistant-hass.home-assistant.svc:8123/api
            - --hass-refresh-seconds
            - "300"
          env:
            WYO_WHISPER_HASS_TOKEN_FILE: /secrets/hass_token
            WYO_WHISPER_INITIAL_PROMPT_FILE: /prompt/initial-prompt.txt
```
and under `persistence:`
```yaml
    whisper-hass-token:
      enabled: true
      type: secret
      name: whisper-hass-token
      advancedMounts:
        whisper:
          whisper:
            - path: /secrets
              readOnly: true
    whisper-prompt:
      enabled: true
      type: configMap
      name: whisper-prompt
      advancedMounts:
        whisper:
          whisper:
            - path: /prompt
              readOnly: true
```
The image repository is private, so the homelab Renovate cannot read its tags: drop the
`# renovate:` hint above (or add a host rule with a `read:package` token) and bump the tag by hand after
each canary.

Notes: the token file is the Vault value as stored; the server may not strip a trailing newline
(**hypothesis**, check in the first `--debug` log). Keep `--model` and `--language` explicit (3.x
defaults them to `auto`). The existing probes (TCP 10300) still work.

**C4. Verify** (the design doc's phase 1 gate): the pod starts without re-downloading the model
(**hypothesis**: 3.x reuses `/data/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo`; if it
starts downloading, the startup probe's 150 s will kill it, so first run with `--local-files-only` or
raise `failureThreshold` for the first start); `--debug` for a few minutes to read the composed prompt
(verbs first, then names, dropped-name count), then remove `--debug`; STT p50 within 0.1 s of 0.86 s;
the owner's spot check on 20 home commands.

**C5. When to do it:** off-hours. `Recreate` plus the model load makes the STT unavailable for the
duration of the start (about the probe's 150 s worst case).

### Phase D: rollback to 2.4.0

1. `git revert` the cutover commit (one revert restores image, args, env, the mounts; do not revert
   only the image, because 2.4.0 rejects `--hass-api` and `--hass-refresh-seconds`). Push with the
   owner's yes and let ArgoCD sync; `Recreate` brings 2.4.0 back.
2. `pando85/wyoming-whisper:2.4.0` stays pullable from Docker Hub (tag dated 2025-02-28) and is still
   in `prusik`'s containerd cache. Do not delete the Docker Hub tag until the new image has run for a few weeks.
3. The model volume needs no repair: 3.x only adds files under `/data` (Xet cache), 2.4.0 ignores them.
4. If only the hint list misbehaves (echoed names, odd transcripts), do not roll back the image: drop
   `--hass-token` handling by deleting `WYO_WHISPER_HASS_TOKEN_FILE`, or revoke the token in Home
   Assistant (the server falls back to the static prefix on 401, per the design doc).
5. Remove the `whisper-hass-token` ExternalSecret and the token itself only when abandoning the
   feature.
6. Image-level rollback (previous tag, older CT2 base if sm_61 fails) is in the image repository's
   `docs/rollback.md`.

## Who does what

| Step | Owner | Agent, with approval |
| --- | --- | --- |
| Create the packages token (`write:package`), store it in `pass`, set the repository secret | yes | no (an agent must not mint or see it) |
| Vault token for `--hass-token` | done 2026-11-05 | read-only check of the path only |
| First tag `v3.8.1.1` and the package cleanup rule | approve, UI for the rule | tag push with approval |
| Canary controller commit, cutover commit, ArgoCD sync | approve each push | yes (commit, push, sync) |
| `kubectl exec`/port-forward in the canary pod, voice clip for the test | yes, or approve | only with approval |
| Spot check of 20 commands, go or no-go | yes | no |
| Docs and compatibility record of results | no | yes |

## Cluster-side risks

- **GPU, driver, sm_61 and the runner disk**: see the image repository's `docs/risks.md`. A driver or GPU
  operator bump past 580 breaks this card for every GPU pod.
- **Circularity.** Forgejo and the STT pod both run on `prusik`. A cold start after a node reboot with an
  empty containerd cache waits for Forgejo; the digest pin and `IfNotPresent` keep this a retry, not an outage.
- **Model cache reuse and token newline** (C3, C4) are untested hypotheses with a cheap check in the first start.
- **Version jump** 2.4.0 to 3.8.1 (also `faster-whisper`, `ctranslate2`, `wyoming`, Python, CUDA): compare
  p50 and the 20-command check against the 2.4.0 baseline.
- **Admin token in the STT pod** (accepted in the design doc): Home Assistant tokens have no scopes; never
  put the token or entity names in the image repository, its Actions logs or pasted `--debug` output.
- Transcripts are no longer written to the whisper log at INFO (patched to DEBUG in the image); the
  Vector rule that drops voice transcripts before Loki still applies to the old image until the cutover.
