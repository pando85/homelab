# Building the Kivi Whisper STT image on Forgejo

**Status:** plan (nothing executed). No repository, push, workflow run, registry upload, cluster or
Home Assistant change was made while writing this.
**Date:** 2026-11-05
**Goal:** replace `pando85/wyoming-whisper:2.4.0` with an image built on
`wyoming-faster-whisper[hass]==3.8.1`, so the STT step gets the live Home Assistant name list
(`--hass-token`) and the static Spanish verb prefix (`--initial-prompt`), as designed in
[STT hint list and learning from usage](stt-hint-list-and-learning.md) (phase 1).

Evidence labels: **documented** (read in a manifest, source or changelog, version named),
**observed** (read from the cluster, Forgejo or the repositories on the date above),
**inferred** (derived, not run), **hypothesis** (to be tested by a step in this plan).

## 1. What exists today (observed)

### The current image

- `apps/home-assistant/values.yaml`, controller `whisper`: `pando85/wyoming-whisper:2.4.0`, no
  Renovate hint (the `helm-values` manager still sees `repository`/`tag`). Deployment strategy
  `Recreate`, `imagePullPolicy: IfNotPresent`, runtime class `nvidia`, node `prusik`.
- It is built from `pando85/wyoming-addons` `whisper/` (a GitHub **fork** of
  `rhasspy/wyoming-addons`, last push 2025-02-28). **There is no CI**: the repository has no
  `.github/` directory and no Forgejo workflow. The `whisper/Makefile` runs
  `docker buildx build --platform linux/amd64,linux/arm64 --tag pando85/wyoming-whisper:$(VERSION) --push`
  by hand and publishes to **Docker Hub**. Docker Hub shows tags `2.4.0`, `latest` (both
  2025-02-28, 1586 MB compressed per arch), `2.0.0`, `1.1.0`; 14.3k pulls.
- `whisper/Dockerfile`: `FROM ghcr.io/opennmt/ctranslate2:4.5.0-ubuntu20.04-cuda12.2`, installs
  Python 3.8 plus `tokenizers==0.20.3` and `wyoming-faster-whisper` from a GitHub tag tarball, then
  `COPY run.sh` (`python3 -m wyoming_faster_whisper --uri tcp://0.0.0.0:10300 --data-dir /data
  --download-dir /data "$@"`). Nothing is pinned by digest or hash.
- Upstream (`rhasspy/wyoming-addons`, `whisper/Dockerfile`) is now a CPU-only Debian image with
  torch, and `wyoming-faster-whisper` 3.8.1 says its `Dockerfile.gpu` is not published (documented in
  the 3.8.1 README). **No ready-made GPU image of 3.8.1 exists**, so a build is unavoidable.

### The GPU and driver

- `prusik`: GTX 1060 3 GB (compute capability 6.1, Pascal), driver **580.159.03**, CUDA runtime
  reported 13.0, GPU time-slicing with 8 replicas (node labels). GPU memory in use: 1141 MiB of
  3072 MiB (whisper plus the other GPU pods).
- The running pod has `ctranslate2 4.5.0`, `faster-whisper 1.1.0`, `wyoming 1.5.3`,
  `wyoming-faster-whisper 2.4.0`, and the model cache
  `/data/models--mobiuslabsgmbh--faster-whisper-large-v3-turbo` (1.6 GB) on the PVC
  `home-assistant-models`.

### Forgejo and its runners

| Item | Value | Source |
| --- | --- | --- |
| Forgejo | 16.0.7 (API reports `16.0.7+gitea-1.22.0`), rootless, behind `nginx-external`, `proxy-body-size 35G`, 3600 s timeouts, buffering off | `platform/git/values.yaml`, `/api/v1/version` |
| Actions | enabled (`actions.ENABLED: true`) | `platform/git/values.yaml` |
| Container registry | on at `git.grigri.cloud` (`/v2/` answers 401 with a Bearer challenge); many repos have container packages | observed |
| Forgejo data volume | 100 Gi PVC, 75.2 GB dataset, 49.6 GB used, **25.6 GB free** | `df` in the pod |
| Runners | two always-on StatefulSets, both amd64, **identical labels** `ubuntu-latest:host`, `ubuntu-24.04:host`, `docker`; jobs land on either | `platform/ci-runners/resources/` |
| Runner `ci-runner` (prusik) | capacity 1, 12 Gi limit, workspace/cache on a 64 Gi ZFS PVC (73 % used, 18 GB free); its DinD data root is on prusik's root disk (83 % used, 74 GB free), which also holds etcd | `df` in the pods |
| Runner `ci-runner-amd64` (`k8s-amd64-1`) | capacity 2, hostPath on the node's single 113.8 GiB root disk (59 % used, 44 GB free); has `docker-gc` thresholds 60/70/78 % | `platform/ci-runners`, `df` |
| Build engine | `ubuntu-latest` is **host mode** on a runner that has the `docker` CLI and `buildx` v0.38.1 talking to a `docker:29.9.2` DinD sidecar (privileged). No kaniko, no standalone buildkit | statefulsets, `buildx version` |
| KEDA | `trigger-authentication.yaml` and `scale-job.yaml` exist but the `ScaledJob` is **commented out** of `kustomization.yaml`; there is no autoscaling, the two StatefulSets are the capacity | `platform/ci-runners/kustomization.yaml` |
| Known hazards | CI fills the node root disk and kubelet evicts everything on it ([ci-runner-amd64-disk-full](../troubleshooting/ci-runner-amd64-disk-full.md)); fresh volumes are root-owned ([ci-runner-workspace-permission-denied](../troubleshooting/ci-runner-workspace-permission-denied.md), fixed by the init container) | docs |

### The reference pattern for "build an image on Forgejo, push to its registry"

- `multivara/calypso` (private org) `.forgejo/workflows/release-and-deploy.yml`: `runs-on:
  ubuntu-latest`; `REGISTRY` taken from `github.server_url`; `echo "$FORGEJO_PACKAGES_PAT" | docker
  login "$REGISTRY" -u "$GITHUB_ACTOR" --password-stdin`; plain `docker build` / `docker tag` /
  `docker push` with retries; image names `git.grigri.cloud/<org>/<repo>-<image>:<sha>`; content-hash
  retag to skip rebuilds. Its images are about 7 GB each, so a 5 GB CUDA image is within what these
  runners already do. (The `docker.yml` workflow in the same repo only lints.)
- Other Forgejo repos with Actions (`docfav-mcp`, `gitops`) use `.forgejo/workflows/*.yml`, `actions/checkout@v7`,
  and a scheduled `renovate.yml` with `RENOVATE_PAT` and `RENOVATE_GITHUB_COM_TOKEN` repository secrets.
- Secrets on the packages path are a repo secret named `FORGEJO_PACKAGES_PAT` (`fj actions secrets list -r <repo>`).
- No repo in this homelab repository pushes to Forgejo's registry; the homelab pipeline for its own
  first-party images is GitHub Actions to `ghcr.io/pando85/*` (for example
  `battery-optimizer`, with `images.yml`, a smoke test, `docker/build-push-action`, and the homelab
  pinning `tag@sha256`).

### How the cluster pulls from `git.grigri.cloud` (observed)

- Pods in `calypso` and `keycloak` run `git.grigri.cloud/multivara/*` images with **no
  `imagePullSecrets` and no `dockerconfigjson` Secret** in the cluster. The node's containerd
  has `certs.d/git.grigri.cloud/hosts.toml` with an `authorization` header (a read-only
  `PAT_packages_read` from the owner's password store), templated by
  `metal/roles/k3s/templates/host.toml.j2` and applied by the `k3s` Ansible role to every node.
- Consequence: **any package on this Forgejo is pullable by every node with no change in this
  repository**, whether the owner is public or private. No pull Secret is needed for the whisper pod.
  (Anonymous pulls are not needed, but would also work for a package whose owner is public; the
  owner namespace `agil` is public, the orgs `multivara` and `grigri` are private.)

### CTranslate2, CUDA and Pascal (sm_61)

- PyPI today (2026-11-05): `ctranslate2` **4.8.5** (2026-11-02), `faster-whisper` **1.2.1**,
  `wyoming-faster-whisper` **3.8.1** (needs `faster-whisper>=1.2.1`, `wyoming>=1.10.2`,
  `pysilero-vad>=3.4.0`; extra `hass` = `aiohttp`; Python `>=3.8`). `faster-whisper` 1.2.1 accepts
  `ctranslate2>=4.0,<5`.
- CTranslate2 4.8.5: its Docker image and wheels are built with **CUDA 12.8 and cuDNN 9**
  (`docker/Dockerfile`: `nvidia/cuda:12.8.1-cudnn-devel-ubuntu22.04`;
  `python/tools/prepare_build_environment_linux.sh`: cuda 12.8) and `CUDA_ARCH_LIST=Common`
  (documented). In `CMakeLists.txt` the "Common" list is CMake's own, and the extra `3.5`/`5.0`
  entries are added only for CUDA 11, so for CUDA 12 it is CMake's common list, which contains 6.1
  (inferred from CMake's `select_compute_arch`; **not yet verified on the binary**, see step 1.3).
  Nothing in the 4.6 to 4.8.5 changelog mentions dropping an architecture.
- Published base image: `ghcr.io/opennmt/ctranslate2:4.8.5-ubuntu22.04-cuda12.8`, amd64,
  digest `sha256:b3589a3b742ad71aacf40ae34d30503bbcbfb8988bd04f48d698a4a8a38aaf63`, 1818 MB compressed
  (the current base is 1503 MB). It carries cuBLAS 12.8, cuDNN 9, the `ctranslate2` Python wheel and
  Python 3.10. Tags go up to `4.8.5-ubuntu22.04-cuda12.8` (observed via the ghcr tag list).
- Driver 580 supports CUDA 12.8 and, per NVIDIA's CUDA 13 notes (not re-read here), is the last
  branch that supports Pascal. **Do not use a CUDA 13 base**, and treat a future driver bump to 590 or
  later as the real end of this GPU (independent of this image).

## 2. Options compared

| | (a) Keep GitHub fork, hand build to Docker Hub | (b) New Forgejo repo, Forgejo Actions, Forgejo registry | (c) GitHub Actions to `ghcr.io/pando85/...` |
| --- | --- | --- | --- |
| What | Edit `whisper/Dockerfile` and `Makefile` in the fork, `make all` from the owner's laptop | New small repo with the Dockerfile, a workflow and a lock file; push builds on tag | Add `.github/workflows/images.yml` to the fork (or a new GitHub repo), like `battery-optimizer` |
| Effort | Lowest now, but repeated by hand every bump | Medium once (repo, secret, workflow), then a tag per release | Medium once, then a tag per release |
| Reproducible, auditable | No (laptop build, no log, no provenance) | Yes (job log, pinned base digest, locked pip set) | Yes (plus GitHub provenance attestation as `battery-optimizer` does) |
| Image size | about 5 GB uncompressed, about 2 GB compressed (**inferred**: base 1818 MB plus about 0.1 to 0.3 GB of wheels; measure on the first build) | same | same |
| Build time | n/a | pull base about 1.8 GB plus pip install: a few minutes on a warm runner (hypothesis; calypso builds of 7 GB images take about 1 to 8 min with cache) | similar on GitHub runners |
| Runner disk risk | none | **yes**: 5 GB of layers on prusik's root disk (etcd shares it) or on `k8s-amd64-1` (history of DiskPressure evictions) | none for the homelab |
| Registry | Docker Hub: anonymous pull rate limits apply to the node IP; Hub is outside the cluster | Forgejo: **in cluster, on `prusik` too**; pull auth already solved (node PAT); adds about 2 GB to a volume with 25.6 GB free | ghcr.io: no auth for public, no rate limit problem; same pattern as `battery-optimizer`, `forgejo-runner-gc`, `gearr` |
| Public or private | public | **owner-visibility decides**: a repo under the public user namespace gives a public package; an org package is private. The nodes can pull either | public |
| Renovate | the homelab Renovate (GitHub) sees the tag on Docker Hub; the image's own inputs (base, wheels) are not tracked | homelab Renovate (GitHub) reads the tag **anonymously** from `git.grigri.cloud` only if the package is public (inferred, test after the first push); the image repo needs its own Renovate run (the `docfav-mcp` `renovate.yml` pattern) | both work natively; Renovate already runs on GitHub |
| Supply chain and pinning | none today | base by digest, wheels by hash, workflow actions by version; PAT scoped to packages | same, plus attestation |
| Outage coupling | none | pod pulls from the same node's Forgejo; image is cached (`IfNotPresent`) and digest-pinned, so only a node with an empty cache is exposed | none |
| Fits the request | no | **yes** | no |

(d) Pull-mirroring `wyoming-addons` into Forgejo: rejected. A pull mirror is read-only, so it cannot
hold the workflow, and it carries 11 unrelated add-ons. (e) Using upstream `rhasspy/wyoming-whisper`:
rejected, it is CPU only (section 1).

### Recommendation

**(b), a new small public repo under the owner's personal Forgejo namespace**, seeded from the fork's
`whisper/` directory (MIT licence, keep the licence file), built by a Forgejo Actions workflow on **tag
push only** (plus manual dispatch), pushed to `git.grigri.cloud/agil/wyoming-whisper`.

Why: it is what was asked; the runners already push 7 GB images; the cluster needs no pull secret; a
public owner namespace makes the package readable by homelab Renovate with no credentials. Why tags
only: the image changes rarely, and every build costs 5 GB on a node whose disks have evicted pods
before. **Fallback:** if the first builds trip the runner disk alarms, move the same Dockerfile and
a copy of the workflow to GitHub and publish to `ghcr.io/pando85/wyoming-whisper` (option c; the
homelab change is only the image name). Option (a) is not worth keeping: it has no log of what was
built, and the new Dockerfile needs a lock file anyway.

## 3. The plan

Every step marked **[owner]** needs the owner. Steps marked **[approval]** can be done by an agent
after an explicit yes (pushes, registry writes, live changes). Read-only checks need no approval.

### Phase A: build and publish the image

**A1. Local pre-flight (no cluster, no push)** [owner or agent in a scratch dir]

1. Seed a working tree from the fork and keep its licence:
   ```sh
   git clone --depth 1 https://github.com/pando85/wyoming-addons /tmp/wa
   mkdir -p ~/src/wyoming-whisper && cd ~/src/wyoming-whisper && git init -b main
   cp /tmp/wa/whisper/{Dockerfile,run.sh,CHANGELOG.md} . && cp /tmp/wa/LICENSE.md .
   ```
2. Replace `Dockerfile` with (versions as of 2026-11-05; Renovate hints so a later bump is a PR):
   ```dockerfile
   # renovate: datasource=docker depName=ghcr.io/opennmt/ctranslate2 versioning=loose
   FROM ghcr.io/opennmt/ctranslate2:4.8.5-ubuntu22.04-cuda12.8@sha256:b3589a3b742ad71aacf40ae34d30503bbcbfb8988bd04f48d698a4a8a38aaf63

   # renovate: datasource=pypi depName=wyoming-faster-whisper
   ARG WYOMING_FASTER_WHISPER_VERSION=3.8.1

   # The base ships ctranslate2 (CUDA 12.8, cuDNN 9) in the system Python 3.10 and an
   # ENTRYPOINT for the CT2 translator; both are replaced below.
   ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
       HF_HOME=/data XDG_CACHE_HOME=/data

   WORKDIR /usr/src
   COPY requirements.txt ./
   # Locked, hashed install (see A1.3). `hass` extra = aiohttp, needed for --hass-token.
   RUN pip3 install --require-hashes -r requirements.txt \
    && python3 -c "import ctranslate2, faster_whisper, aiohttp, wyoming_faster_whisper.hass_api; \
print('ctranslate2', ctranslate2.__version__)"

   COPY run.sh /run.sh
   WORKDIR /
   EXPOSE 10300
   ENTRYPOINT ["bash", "/run.sh"]
   ```
   `run.sh` stays as it is. Keep `HF_HOME=/data` so the Xet cache of 3.x lands on the PVC
   (3.x does the same in its own Dockerfile). The old `tokenizers==0.20.3` workaround was for Python
   3.8 and is dropped.
3. Generate the lock file once, on a machine with `uv`, for the image's Python and platform, and commit it:
   ```sh
   echo 'wyoming-faster-whisper[hass]==3.8.1' > requirements.in
   uv pip compile requirements.in --python-version 3.10 --python-platform x86_64-manylinux_2_28 \
     --generate-hashes --constraint <(printf 'ctranslate2==4.8.5\n') -o requirements.txt
   ```
   The lock must contain `ctranslate2==4.8.5` (so pip sees the base's copy as satisfied) and an `av`
   release that has a Python 3.10 wheel (3.8.1's dependency `faster-whisper` leaves `av` open).
4. `docker build -t wyoming-whisper:local .` on the owner's workstation and run the import check
   inside it. Record the real compressed and uncompressed size and build time for the compatibility
   record (this replaces the estimates above).
5. **sm_61 check without a GPU** (closes the main inference above): list the architectures inside
   the shipped library, using a CUDA devel image only for `cuobjdump`:
   ```sh
   docker run --rm --entrypoint sh wyoming-whisper:local -c \
     'python3 -c "import ctranslate2,os;print(os.path.dirname(ctranslate2.__file__))"; ls /usr/local/lib/python3.10/dist-packages/ctranslate2.libs 2>/dev/null'
   # copy the libctranslate2*.so out (docker cp) and run:
   docker run --rm -v "$PWD/out:/o:ro" nvidia/cuda:12.8.1-devel-ubuntu22.04 \
     cuobjdump --list-elf /o/libctranslate2*.so | grep -c sm_61
   ```
   Expect `sm_61` ELF entries (or `compute_61` PTX). If absent, stop: pin an older CT2 base
   (`4.5.0-ubuntu20.04-cuda12.2` is what runs today) and record the finding.

**A2. Create the repo** [approval]
```sh
fj -H git.grigri.cloud repo create wyoming-whisper \
  -d "Wyoming faster-whisper STT server, CUDA 12.8 image for Pascal GPUs (forked from rhasspy/wyoming-addons, MIT)" \
  --remote origin --ssh --push        # run inside ~/src/wyoming-whisper after the first commit
```
No `-P`: public. Public is safe: the Dockerfile contains no home data and the Home Assistant token is
only ever given at run time. Do not put the owner's hint list or entity names in this repo.

**A3. Workflow** [approval to push]. File `.forgejo/workflows/image.yml`:
```yaml
name: Image
on:
  push:
    tags: ["v*"]
  pull_request:
    paths: [Dockerfile, run.sh, requirements.txt, .forgejo/workflows/image.yml]
  workflow_dispatch:

env:
  IMAGE: git.grigri.cloud/${{ github.repository }}

jobs:
  image:
    runs-on: ubuntu-latest
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v7

      - name: Build (amd64 only, the GPU node is amd64)
        run: docker build --pull -t "$IMAGE:ci" .

      - name: Smoke test (no GPU on the runners)
        run: |
          docker run --rm --entrypoint python3 "$IMAGE:ci" -c \
            "import ctranslate2, faster_whisper, wyoming_faster_whisper.hass_api as h; print(ctranslate2.__version__)"
          docker run --rm "$IMAGE:ci" --help | grep -q -- --hass-token

      - name: Push (tags only)
        if: startsWith(github.ref, 'refs/tags/v')
        env:
          PAT: ${{ secrets.FORGEJO_PACKAGES_PAT }}
        run: |
          VERSION="${GITHUB_REF_NAME#v}"
          echo "$PAT" | docker login git.grigri.cloud -u "$GITHUB_ACTOR" --password-stdin
          docker tag "$IMAGE:ci" "$IMAGE:$VERSION"
          docker push "$IMAGE:$VERSION"
          echo "pushed $(docker inspect --format '{{index .RepoDigests 0}}' "$IMAGE:$VERSION")" | tee -a "$GITHUB_STEP_SUMMARY"

      - name: Free the runner disk
        if: always()
        run: docker image rm -f "$IMAGE:ci" "$IMAGE:${GITHUB_REF_NAME#v}" || true
```
Notes: no `latest` tag is pushed (the homelab pins versions); the last step is the lesson from
[ci-runner-amd64-disk-full](../troubleshooting/ci-runner-amd64-disk-full.md) (calypso's builds left
7 GB images behind). Tag scheme: `<wyoming-faster-whisper version>.<build revision>`, for example
`v3.8.1.1`; a change to the base or the lock with the same upstream version bumps the last number.

**A4. Registry credentials** [owner]
1. In Forgejo, **Settings, Applications**: new access token named `wyoming-whisper-ci-packages-write`,
   scope `write:package` only (add `read:package` if the UI asks). Do not reuse the calypso token (it
   belongs to an org-scoped secret and is wider than needed). Save it where the other PATs live
   (`pass`), mode `0600`, never in the repo.
2. Hand it to the repo without printing it:
   ```sh
   fj -H git.grigri.cloud actions secrets create -r agil/wyoming-whisper FORGEJO_PACKAGES_PAT "$(cat /path/to/0600-file)"
   ```
   (An agent may run this once the owner has put the file in place and approved.)
3. Optional later: try dropping the PAT for the job's own token (`github.token`). Whether Forgejo
   16.0.7 lets the automatic token push to the owner's registry is a **hypothesis**; calypso uses a PAT.

**A5. First build** [approval]
```sh
git tag v3.8.1.1 && git push origin v3.8.1.1      # or: fj actions dispatch (no push)
fj -H git.grigri.cloud actions tasks -r agil/wyoming-whisper   # follow it
```
Watch the runner while it builds: `df -h /var/lib/docker` on the pod's node, `docker-gc` log lines
in Loki (`{namespace="ci-runners",container="docker-gc"}`). Record the pushed digest (from the job
summary) and the package size (`fj`/UI package page). Then set the owner's Forgejo **package
cleanup rule** (user Settings, Packages) to keep the last 3 versions of this image, so the 25.6 GB
free on the Forgejo volume is not eaten by old versions (UI only; no API in this Forgejo version
that I found).

**A6. Renovate for the image repo** (optional, after A5) [approval]
Copy `.forgejo/workflows/renovate.yml` from the `docfav-mcp` repo, add `.forgejo/renovate-config.json`
(`extends: config:recommended`, `minimumReleaseAge: 3 days`, the regex manager for the Dockerfile `ARG`
hint above, `pip-compile` or `pip_requirements` for `requirements.txt`), and the secrets
`RENOVATE_PAT` and `RENOVATE_GITHUB_COM_TOKEN` (owner). A Renovate PR changes the version; **a human
tags the release**, because every release must go through the GPU canary below.

### Phase B: GPU canary on `prusik` (before touching the real STT)

The card has 3 GB with 1.1 GB in use, so do **not** run two large-v3-turbo instances. A tiny model
needs well under 300 MiB.

1. [approval, GitOps] Commit a temporary controller `whisper-canary` in
   `apps/home-assistant/values.yaml`: the new image, args `--model tiny-int8 --language es --device
   cuda --compute-type int8 --debug`, `nvidia.com/gpu: "1"`, `runtimeClassName: nvidia`, no Service
   and no Ingress, `emptyDir` for `/data` (it downloads the tiny model; needs egress to Hugging Face).
   ArgoCD syncs it.
2. [owner or approved exec] Read-only checks in the pod: `python3 -c "import ctranslate2 as c;
   print(c.get_cuda_device_count(), c.get_supported_compute_types('cuda'))"` (expect device count 1
   and `int8` in the list), and the pod log (`--debug` prints the loaded device and, with a token,
   the prompt). A Wyoming transcribe of a short Spanish WAV through `kubectl port-forward` is the end
   to end test; use a clip of the owner's own voice and delete it afterwards.
3. Pass criteria: no CUDA error such as `no kernel image is available for execution on the device`
   (that is the signature of a missing sm_61 build), supported compute types include `int8`, one
   transcript returned. Record the result in this document or the compatibility notes.
4. Remove the canary controller (revert that commit).

### Phase C: cutover (homelab GitOps)

All three changes in one commit `home-assistant: Run whisper 3.8.1 with live Home Assistant names`
(or split into an inert ExternalSecret+ConfigMap commit first), pushed only with the owner's yes.

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
            # renovate: datasource=docker depName=git.grigri.cloud/agil/wyoming-whisper versioning=loose
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
Also add `loose` versioning for this image in `.github/renovate.json5` (the existing rule lists
`/forgejo-runner/` and others; add `/wyoming-whisper/`) because the `x.y.z.n` tag is not semver, and
check that automerge for docker patch updates does not apply: **set `automerge: false` for
`git.grigri.cloud/agil/wyoming-whisper`**, since each bump needs the canary.

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

## 4. Who does what

| Step | Owner | Agent, with approval |
| --- | --- | --- |
| Create the packages PAT (`write:package`), store it in `pass`, `0600` file | yes | no (an agent cannot and must not mint or see it) |
| Vault token for `--hass-token` | done 2026-11-05 | read-only check of the path only |
| Create the repo, push seed files and workflow, push tag `v3.8.1.1`, set the repo secret | approve | yes (`fj`, `git`) |
| Package cleanup rule (keep last 3) | yes (UI) | no |
| Renovate secrets for the image repo (`RENOVATE_PAT`, `RENOVATE_GITHUB_COM_TOKEN`) | yes | no |
| Local build, size, `cuobjdump` sm_61 check | optional | yes (no cluster) |
| Canary controller commit, cutover commit, ArgoCD sync | approve each push | yes (commit, push, sync) |
| `kubectl exec`/port-forward in the canary pod, voice clip for the test | yes, or approve | only with approval |
| Spot check of 20 commands, go or no-go | yes | no |
| Docs and compatibility record of results | no | yes |

## 5. Risks and unknowns

1. **sm_61 in the CT2 4.8.5 binary** is inferred from build settings, not verified. Gates: A1.5
   (`cuobjdump`) and Phase B. If missing, fall back to the base that runs today
   (`4.5.0-ubuntu20.04-cuda12.2`, Python 3.8) and pin `faster-whisper` to the newest version that
   works with it; 3.8.1 needs `faster-whisper>=1.2.1`, which accepts CT2 4.x, so only the base changes.
2. **Driver and CUDA ceiling.** Driver 580 is the last Pascal branch and CUDA 13 images drop Pascal.
   The image must stay on CUDA 12.x. A GPU operator or driver bump past 580 breaks this card for every
   GPU pod, not only whisper.
3. **Runner disk.** A 5 GB build on prusik's runner (root disk shared with etcd, 83 % used) or on
   `k8s-amd64-1` (past DiskPressure outages) is the largest operational risk. Both runners have the same labels, so the
   job cannot be steered to the roomier one. Mitigations: tag-only builds, the cleanup step,
   `docker-gc`, watching `df` during A5. If either alarm fires, use the GitHub fallback.
4. **Forgejo volume.** 25.6 GB free; each version adds only changed layers (the 1.8 GB base is shared),
   but calypso also writes there. Apply the cleanup rule.
5. **Circularity.** Forgejo and the STT pod both run on `prusik`. Cold start after a node reboot with an
   empty containerd cache would wait for Forgejo; the digest pin and `IfNotPresent` keep this to a
   retry, not an outage.
6. **Model cache reuse and token newline** (C3 and C4) are untested hypotheses; both have a cheap
   check in the first start.
7. **Renovate reaching the registry anonymously** (public owner namespace) is inferred. Test after A5
   with the token-less flow (`GET /v2/token?service=container_registry&scope=repository:agil/wyoming-whisper:pull`,
   then `tags/list`). If it fails, add a Renovate host rule for `git.grigri.cloud` with a
   `read:package` token, or keep the image tag bumps manual.
8. **Version jump.** 2.4.0 to 3.8.1 also moves `faster-whisper` 1.1.0 to 1.2.1, `ctranslate2` 4.5.0 to
   4.8.5, `wyoming` 1.5.3 to 1.10.x, Python 3.8 to 3.10 and CUDA 12.2 to 12.8. Accuracy and latency can
   change even without a prompt; compare p50 and the 20-command check against the 2.4.0 baseline, and
   keep the baseline numbers from the study.
9. **Admin token in the STT pod** (already accepted in the design doc): Home Assistant tokens have no
   scopes. The image repo is public, so never put the token or entity names in it, in its Actions
   logs or in `--debug` output that is pasted anywhere.
10. **Supply chain.** Base by digest and wheels by hash remove silent drift but need Renovate (A6) to stay
    current. `actions/checkout@v7` is referenced by tag, as in the other Forgejo repos.
11. **Unreleased upstream 3.9.0** (language reporting only) is not needed; stay on 3.8.1 until a reason appears.
