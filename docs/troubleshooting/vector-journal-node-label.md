# Vector Journal node_name Label Derived from Wrong Field

## Problem

Journal logs shipped to Loki carried an incorrect or empty `node_name` label. Loki queries
filtering by `node_name` for systemd-journal streams returned no results or matched the wrong
node.

## Root Cause

The `journal_labels` remap transform derived `node_name` from the raw journald field
`._HOSTNAME` instead of Vector's normalized `.host` field.

```
.node_name = string(._HOSTNAME) ?? ""
```

Vector's `journald` source populates `.host` from the journal's `__HOSTNAME` metadata (the
boot-time machine hostname). The raw `_HOSTNAME` journal field can differ from `.host` when
the hostname changes after boot, when running inside containers, or when journald records a
different value than what Vector resolves. This caused the `node_name` Loki label to diverge
from the actual Kubernetes node name used by the `kubernetes_logs` pipeline
(`kubernetes.pod_node_name`).

## How to Diagnose

Check the VRL transform in the Vector agent config:

```bash
kubectl --context=grigri -n loki get configmap vector-agent -o jsonpath='{.data.vector\.yaml}' \
  | grep -A3 'journal_labels'
```

If `.node_name` references `._HOSTNAME` without checking `.host` first, the transform is
affected.

Compare journal `node_name` values against pod `node_name` values in Loki:

```logql
{job="systemd-journal", node_name!=""}
{job="kubernetes_logs", node_name!=""}
```

## Fix

Use Vector's `.host` as the primary source with `._HOSTNAME` as a safe fallback:

```
.node_name = string(.host) ?? string(._HOSTNAME) ?? ""
```

## Regression Test

Inline Vector config tests are included in `system/loki/vector-agent.yaml` under
`customConfig.tests`. Render with chart version 0.58.0 and run the tests with the pinned Vector
image, 0.54.0:

```bash
helm template vector-agent vector --repo https://helm.vector.dev --version 0.58.0 --namespace loki \
  --values system/loki/vector-agent.yaml \
  | python3 -c "
import sys, yaml
for doc in yaml.safe_load_all(sys.stdin):
    if doc and doc.get('kind') == 'ConfigMap':
        print(doc['data']['vector.yaml'])
" > /tmp/vector-config.yaml

docker run --rm -v /tmp/vector-config.yaml:/etc/vector/vector.yaml:ro \
  timberio/vector:0.54.0-debian test /etc/vector/vector.yaml
```

Three cases are covered:

| Test                                      | Verifies                                    |
|-------------------------------------------|---------------------------------------------|
| `journal_node_name_prefers_host`          | `.host` wins over `._HOSTNAME`              |
| `journal_node_name_fallback_to__HOSTNAME` | `._HOSTNAME` used when `.host` is absent    |
| `journal_node_name_empty_when_both_missing`| Empty string when neither field exists      |
