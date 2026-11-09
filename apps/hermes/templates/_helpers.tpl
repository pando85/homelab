{{/*
Single gate for the opt-in MCP gateway client.

``global`` because it is the only value path Helm shares with subcharts, and it
stays a real boolean: every ``enabled`` field of the app-template subchart is
locked to ``type: boolean`` by its ``values.schema.json``, so the alternatives
(templated ``enabled`` strings, or rewriting ``.Values`` from a parent template)
are both impossible - verified against the vendored chart source and with render
probes. Reading it through ``dig`` means a values tree that has no ``global``
block at all (e.g. the pre-feature baseline) resolves to disabled instead of
erroring on a nil pointer, and a non-boolean value fails the render rather than
being quietly coerced.
*/}}
{{- define "hermes.mcpGateway.enabled" -}}
{{- $value := dig "mcpGateway" "enabled" false .Values.global -}}
{{- if not (kindIs "bool" $value) -}}
{{- fail "global.mcpGateway.enabled must be a boolean (true or false), not a string" -}}
{{- end -}}
{{- $value -}}
{{- end -}}

{{/*
Security context for the MCP gateway helper containers.

Only ever referenced from templates gated on ``global.mcpGateway.enabled``, so
nothing here reaches the Hermes pod.

The helpers run as uid 0 because that is the uid the Hermes agent container
already runs as and the uid that owns ``$HERMES_HOME`` on ``hermes-data``: a
non-root helper cannot write a root-owned 0755 directory (reproduced offline in
tests/test_helper_container_runtime.py, which shows the uid-10000 write failing
with EACCES where uid 0 succeeds). Making it non-root would mean either chowning
agent data (``fsGroup`` rewrites ownership recursively on ZFS volumes) or
putting the credential store somewhere Hermes does not read. So instead of
pretending this pod is unprivileged, it is capped: read-only root filesystem,
all capabilities dropped, no privilege escalation, no Kubernetes API access
(``automountServiceAccountToken: false``) and one narrow job - two files in one
directory on one PVC.
*/}}
{{- define "hermes.mcpGateway.containerSecurityContext" -}}
{{- $h := dig "helper" dict (dig "mcpGateway" dict .Values.global) }}
securityContext:
  runAsUser: {{ $h.runAsUser }}
  runAsGroup: {{ $h.runAsGroup }}
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop:
      - ALL
{{- end -}}
