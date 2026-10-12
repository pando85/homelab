# ArgoCD Kustomize Helm OCI Charts Return 401

## Problem

After the repo-server moved to Argo CD v3.5.4 (2026-10-12), every Application that renders a
private OCI Helm chart through kustomize `helmCharts` (`calypso` from `git.grigri.cloud`,
`isidoro` from `tea.millaguie.net`) went sync status `Unknown` with:

```
failed to perform "FetchReference" on source: GET "https://<registry>/v2/<repo>/manifests/<ver>":
response status code 401: Unauthorized ... unable to run: 'helm pull --untar ... oci://...'
with env=[HELM_CONFIG_HOME=/helm-working-dir ...]
```

Workloads kept running the last successfully synced revision; new chart versions were never
deployed. Running the same `helm pull` by hand inside the repo-server worked, and the Vault
credentials were valid.

## Root Cause

The OCI registry credentials are a Docker-style `config.json` (ExternalSecret
`argocd-repo-creds-helm-git-grigri-cloud-workaround`) mounted at
`/helm-working-dir/registry/config.json`. Helm used to find it as
`$HELM_CONFIG_HOME/registry/config.json`.

Argo CD v3.5.4 fixed GHSA-fw5c-w8rc-j7fx by putting a `helm` wrapper script
(`/tmp/argocd-helm-wrapper-*/helm`) first on the PATH of `kustomize build`. The wrapper always
replaces `HELM_CONFIG_HOME`, `HELM_DATA_HOME` and `HELM_PLUGINS` with an empty Argo CD-owned
directory, so `helmGlobals.configHome` and the repo-server's `HELM_CONFIG_HOME` are ignored and helm
pulls anonymously. The kustomize error message still prints the old `HELM_CONFIG_HOME`, because
kustomize logs the env it set, not the env the wrapper exec's helm with.

## How to Diagnose

```bash
kubectl --context=grigri -n argocd get app <name> -o jsonpath='{.status.conditions}'
POD=$(kubectl --context=grigri -n argocd get pod -l app.kubernetes.io/name=argocd-repo-server -o name | head -1)
# Reproduce through the wrapper exactly as kustomize does (prints only the result line):
kubectl --context=grigri -n argocd exec "$POD" -- sh -c \
  'W=$(ls -d /tmp/argocd-helm-wrapper-* | head -1); d=$(mktemp -d);
   PATH=$W:$PATH helm pull --untardir $d oci://git.grigri.cloud/multivara/helm-charts/calypso \
   --version <ver> 2>&1 | tail -1; rm -rf $d'
```

A 401 through the wrapper but a success with plain `helm pull` confirms this cause.

## Fix / Workaround

The wrapper does not touch `DOCKER_CONFIG`, and helm falls back to the Docker credential store when
its own registry config has no entry. `bootstrap/argocd/values.yaml` sets
`DOCKER_CONFIG=/helm-working-dir/registry` on the repo-server so helm reads the mounted
`config.json`. `HELM_REGISTRY_CONFIG` would also work, but it would make `helm registry login` for
native Helm-OCI Applications write into the read-only secret mount.

The `config.json` must still carry an `auths` entry for every private OCI registry that kustomize
pulls charts from. The `ArgoCDAppSyncUnknown` alert (`bootstrap/argocd/resources/prometheus-rules.yaml`)
fires when an Application cannot generate manifests for 30 minutes.

## Removal

This workaround can be removed once argoproj/argo-cd#28678 ships natively — that PR passes OCI
registry credentials through to kustomize's helm invocations without relying on `DOCKER_CONFIG`.
When the fix lands, remove the `DOCKER_CONFIG` env entry from `bootstrap/argocd/values.yaml` and
verify that `helm pull oci://...` succeeds through the wrapper without it.
